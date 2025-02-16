''' transit_timer'''

import csv
import copy
import datetime
import heapq
import logging
import os
import re
import time

from collections import defaultdict
from operator import itemgetter

import asyncio
import aiohttp
import humanize
import pycron
import pytz
import requests
import yaml

from flask import Flask, render_template, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from google.transit import gtfs_realtime_pb2
from requests.exceptions import ReadTimeout, RequestException

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Initialize Flask app
templates_folder = os.path.join(os.path.dirname(os.path.realpath(__file__)), "templates")
app = Flask(__name__, template_folder=templates_folder)

# Set up Flask-Limiter for rate limiting
limiter = Limiter(get_remote_address, app=app, default_limits=["10 per second"])

# Cache static GTFS data at app startup
gtfs_static_data_cache = {}

def load_gtfs_static_data(gtfs_static_data_folder):
    ''' Load static GTFS data into cache '''
    data_dict = {}
    for file_name in os.listdir(gtfs_static_data_folder):
        file_path = os.path.join(gtfs_static_data_folder, file_name)
        if os.path.isfile(file_path):
            with open(file_path, encoding="utf-8") as file:
                data = list(csv.DictReader(file))
                data_dict[os.path.splitext(os.path.basename(file_path))[0]] = data
    return data_dict

def get_gtfs_static_data(gtfs_static_data_folder):
    ''' Return cached static GTFS data '''
    if gtfs_static_data_folder not in gtfs_static_data_cache:
        gtfs_static_data_cache[gtfs_static_data_folder] = \
        load_gtfs_static_data(gtfs_static_data_folder)
    return gtfs_static_data_cache[gtfs_static_data_folder]

def gtfs_lookup(data, column_name, match):
    '''Look up GTFS data with precompiled regex'''
    pattern = re.compile(match)
    return [d_d for d_d in data if pattern.search(d_d[column_name])]

def fetch_gtfs_data(url, retries=3, backoff_factor=2):
    ''' Fetch GTFS real-time data with retries and error handling '''
    attempt = 0
    while attempt < retries:
        try:
            logging.info(f"Fetching {url} (Attempt {attempt + 1})")
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            return response.content

        except ReadTimeout:
            logging.warning(f"Timeout when fetching {url}. Retrying in {backoff_factor ** attempt} seconds...")
            time.sleep(backoff_factor ** attempt)  # Exponential backoff
            attempt += 1

        except RequestException as e:
            logging.error(f"Request error while fetching {url}: {e}")
            break  # Stop retrying if it's another request error

    logging.error(f"Failed to fetch {url} after {retries} attempts.")
    return None  # Return None if all retries fail

async def fetch_gtfs_data_async(url, session):
    ''' Asynchronously fetch GTFS real-time data '''
    try:
        async with session.get(url, timeout=10) as response:
            response.raise_for_status()
            content = await response.read()
            return content
    except Exception as e:
        print(f"Error fetching {url}: {e}")
        return None

async def fetch_and_process_urls(urls, stop_ids):
    ''' Asynchronously fetch and process GTFS-RT feeds '''
    entities = []

    async with aiohttp.ClientSession() as session:
        tasks = [fetch_gtfs_data_async(url, session) for url in urls]
        results = await asyncio.gather(*tasks)

    for url, content in zip(urls, results):
        if content is None:
            continue

        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(content)

        valid_entities = []
        for entity in feed.entity:
            if not entity.HasField("trip_update"):
                continue  # Skip entities without trip updates

            # Filter stop updates
            filtered_updates = [
                update for update in entity.trip_update.stop_time_update
                if update.stop_id.strip().upper() in stop_ids
            ]

            if filtered_updates:
                del entity.trip_update.stop_time_update[:]  # Properly clear list
                entity.trip_update.stop_time_update.extend(filtered_updates)  # Append valid updates
                valid_entities.append(entity)

        entities.extend(valid_entities)

    return entities


def get_stops(settings, gtfs_static_data, transit_type):
    ''' Efficiently fetches and processes GTFS-RT data asynchronously '''
    
    # Fetch API key (for bus only)
    key = ""
    if transit_type == "bus":
        with open(settings["bus_key_file"], "r", encoding="utf-8") as file:
            key = f"?key={file.read().strip()}"

    # Generate URLs with API keys
    urls = [f"{url}{key}" for url in settings["transit_type"][transit_type]["gtfs-rt_urls"]]

    # Convert stop list to **set for O(1) lookups**
    stop_ids = {
        stop["stop_id"].strip().upper()
        for stop_name in settings["transit_type"][transit_type]["stops"]
        for stop in gtfs_lookup(gtfs_static_data["stops"], "stop_name", f"^{re.escape(stop_name)}$")
    }

    # Run async GTFS fetching + processing
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    entities = loop.run_until_complete(fetch_and_process_urls(urls, stop_ids))

    return entities

def clean_entity(stop_ids, entity):
    '''Removes stops we don't care about'''

    entity_copy = copy.deepcopy(entity)
    if entity_copy.HasField("trip_update"):
        for update in entity_copy.trip_update.stop_time_update:
            if update.stop_id not in stop_ids:
                entity.trip_update.stop_time_update.remove(update)

    return entity

def upcoming_stops(stops_to_return, entities):
    '''Return the N upcoming stop updates with optimized sorting'''
    stop_id = {}
    for entity in entities:
        for update in entity.trip_update.stop_time_update:
            stop_id.setdefault(update.stop_id, [])
            heapq.heappush(stop_id[update.stop_id], update.arrival.time)

    for key, value in stop_id.items():
        stop_id[key] = heapq.nsmallest(stops_to_return, value)

def load_settings(starting_point):
    ''' load_settings '''
    current_dir = os.path.dirname(os.path.realpath(__file__))
    starting_point_dir = os.path.join(current_dir, "Starting Points")
    starting_point_file = os.path.join(starting_point_dir, f"{starting_point}.yaml")

    with open(starting_point_file, 'r', encoding="utf-8") as file:
        return yaml.safe_load(file)

def get_arrival_data(settings, transit_type, gtfs_static_data, entity, update):
    ''' Extracts arrival time and stop information '''

    stop_id = update.stop_id.strip().upper()

    # Use dictionary lookups (O(1) time) instead of multiple regex searches
    static_stops = {stop["stop_id"].strip().upper(): stop["stop_name"] for stop in gtfs_static_data["stops"]}
    static_routes = {route["route_id"]: route for route in gtfs_static_data["routes"]}

    # Function to modify trip_id if it matches the regex
    def transform_trip_id(trip_id):
        """ 
        Extracts everything after the first `_` and stops at '..[A-Z]', 
        ensuring only valid characters remain after '..[A-Z]'.
        """
        match = re.search(r'_(\d+_.*?\.\.[A-Z])', trip_id)  # Extract number + text up to `..[A-Z]`
        transformed_id = match.group(1) if match else trip_id  # Keep original if no match

        final_match = re.search(r'(\d+_.*?\.\.[A-Z])', transformed_id)  # Ensure nothing follows `..[A-Z]`
        return final_match.group(1) if final_match else transformed_id  # Keep modified or original

    # Apply transformation to trip_id in GTFS static data
    static_trips = {transform_trip_id(trip["trip_id"]): trip for trip in gtfs_static_data["trips"]}

    stop_name = static_stops.get(stop_id, f"Unknown Stop ({stop_id})")

    if stop_name not in settings["transit_type"][transit_type]["stops"]:
        print(f"Warning: Stop '{stop_name}' (ID: {stop_id}) not found in settings for {transit_type}")
        return None  # Skip processing if stop isn't part of the selected transit type

    arrival_time = update.arrival.time
    arrival_time_seconds = int(arrival_time - time.time())

    # Use humanize.naturaltime to format arrival time
    arrival_time_relative = humanize.naturaltime(datetime.datetime.fromtimestamp(arrival_time))

    route_id = entity.trip_update.trip.route_id
    trip_id = transform_trip_id(entity.trip_update.trip.trip_id)

    # Retrieve trip data
    trip_data = static_trips.get(trip_id, None)

    # Function to determine direction if no trip data
    def get_direction_from_trip_id(trip_id):
        direction_map = {
            "N": "Uptown",
            "S": "Downtown",
            "E": "To East Side",
            "W": "To West Side"
        }
        last_char = trip_id[-1] if trip_id else ""
        return direction_map.get(last_char, "Unknown Direction")

    # Determine trip direction
    trip_direction = trip_data.get("direction_id", None) if trip_data else get_direction_from_trip_id(trip_id)

    trip_headsign = trip_data.get("trip_headsign", f"Route {route_id}") if trip_data else trip_direction

    # Retrieve route details
    route_data = static_routes.get(route_id, {})
    route_short_name = route_data.get("route_short_name", route_id)

    # Use `settings.yaml` direction names if available
    stop_settings = settings["transit_type"][transit_type]["stops"].get(stop_name, {})
    if "direction_id_to_name" in stop_settings:
        trip_direction = stop_settings["direction_id_to_name"].get(trip_direction, trip_headsign)
    else:
        trip_direction = settings.get("direction_id_to_name", {}).get(trip_direction, trip_headsign)

    return {
        "stop_id": stop_id,
        "stop_name": stop_name,
        "arrival_time": arrival_time_relative,  # Humanized arrival time
        "arrival_time_seconds": arrival_time_seconds,
        "route_name": f"{route_short_name} {trip_direction}",
        "route_color": f"#{route_data.get('route_color', 'FFFFFF')}",
        "route_text_color": f"#{route_data.get('route_text_color', '000000')}",
    }


def display_stop(timezone, schedule):
    ''' display_stop '''

    my_timezone = pytz.timezone(timezone)
    my_datetime = datetime.datetime.now().astimezone(my_timezone)

    return bool(pycron.is_now(schedule, my_datetime))

def is_quiet_time(settings):
    ''' is_quiet_time '''
    total_stops = 0
    deleted_stops = 0
    for transit_type in settings["transit_type"]:
        for stop in list(settings["transit_type"][transit_type]["stops"].keys()):
            if not display_stop(settings["timezone"],
                                settings["transit_type"][transit_type]["stops"][stop]["schedule"]):
                del settings["transit_type"][transit_type]["stops"][stop]
                deleted_stops += 1
            total_stops += 1

    return bool(deleted_stops == total_stops)

def group_and_filter_arrivals(arrivals, stops_to_return):
    ''' Efficiently group and return the top N arrivals per stop '''
    grouped_data = defaultdict(list)

    for arrival in arrivals:
        grouped_data[arrival["stop_name"]].append(arrival)

    return [
        item for group in grouped_data.values()
        for item in sorted(group, key=lambda x: x["arrival_time_seconds"])[:stops_to_return]
    ]

def get_available_starting_points():
    ''' Retrieve available starting points and their descriptions '''
    starting_point_dir = os.path.join(os.path.dirname(os.path.realpath(__file__)), "Starting Points")
    starting_points = []

    for file_name in os.listdir(starting_point_dir):
        if file_name.endswith(".yaml"):
            file_path = os.path.join(starting_point_dir, file_name)
            with open(file_path, 'r', encoding="utf-8") as file:
                settings = yaml.safe_load(file)
                starting_points.append({
                    "name": os.path.splitext(file_name)[0],
                    "description": settings.get("description", "No description available"),
                    "transit_types": list(settings.get("transit_type", {}).keys())
                })

    return starting_points

@app.route('/')
def index():
    ''' Display the index page with available starting points '''
    starting_points = get_available_starting_points()
    return render_template("index.html", starting_points=starting_points)

@app.route('/starting_point/<starting_point>')
def starting_point(starting_point):
    ''' starting_point '''
    start_time = time.time()

    settings = load_settings(starting_point)
    transit_types = request.args.getlist('transit_type')

    if not transit_types:
        transit_types = list(settings["transit_type"].keys())

    my_datetime = datetime.datetime.now().astimezone(pytz.timezone(settings["timezone"]))
    arrivals = []

    if is_quiet_time(settings):
        return render_template("quiet_time.html",
                               last_updated=my_datetime.strftime("%Y-%m-%d %I:%M:%S %p"))

    for transit_type in transit_types:
        if transit_type in settings["transit_type"]:
            gtfs_static_data_folder = settings["transit_type"][transit_type]["gtfs_static_data"]
            gtfs_static_data = get_gtfs_static_data(gtfs_static_data_folder)
            entities = get_stops(settings, gtfs_static_data, transit_type)

            for entity in entities:
                if 'trip_update' in entity:
                    for update in entity.trip_update.stop_time_update:
                        arrival_time = update.arrival.time

                        if arrival_time >= time.time():
                            arrival_data = get_arrival_data(settings, transit_type, gtfs_static_data, entity, update)
                            arrivals.append(arrival_data)

    filtered_arrivals = group_and_filter_arrivals(arrivals, settings["stops_to_return"])
    arrivals = sorted(filtered_arrivals, key=itemgetter("arrival_time_seconds", "stop_name"))

    response = render_template("starting_point.html",
                               arrivals=arrivals,
                               last_updated=my_datetime.strftime("%Y-%m-%d %I:%M:%S %p"),
                               elapsed_time=round(time.time() - start_time, 2))

    return response

# Run the app
if __name__ == '__main__':
    app.run(debug=True)
