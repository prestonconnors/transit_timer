''' transit_timer'''

import csv
import copy
import datetime
import heapq
import os
import re
import time

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from operator import itemgetter

import pycron
import pytz
import requests
import yaml

from flask import Flask, render_template, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from humanize import naturaltime
from google.transit import gtfs_realtime_pb2

# Initialize Flask app
templates_folder = os.path.join(os.path.dirname(os.path.realpath(__file__)), "templates")
app = Flask(__name__, template_folder=templates_folder)

# Set up Flask-Limiter for rate limiting
limiter = Limiter(get_remote_address, app=app, default_limits=["10 per second"])

# Cache static GTFS data at app startup
gtfs_static_data_cache = {}

def time_function(func, *args, **kwargs):
    ''' Measure execution time of a function '''
    start_time = time.time()
    result = func(*args, **kwargs)
    elapsed_time = round(time.time() - start_time, 2)
    return result, elapsed_time

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

def fetch_gtfs_data(url):
    ''' Fetch GTFS real-time data and measure time '''
    start_time = time.time()
    response = requests.get(url, timeout=10)
    elapsed_time = round(time.time() - start_time, 2)
    response.raise_for_status()
    return response.content, elapsed_time

def get_stops(settings, gtfs_static_data, transit_type):
    ''' Get Stops with parallel HTTP requests '''
    if transit_type == "bus":
        with open(settings["bus_key_file"], "r", encoding="utf-8") as file:
            key = f"?key={file.read().strip()}"
    else:
        key = ""

    stop_ids = [
        _["stop_id"] for stop_name in settings["transit_type"][transit_type]["stops"]
        for _ in gtfs_lookup(gtfs_static_data["stops"], "stop_name", f"^{stop_name}$")
    ]

    urls = [
        f"{gtfs_rt_url}{key}"
        for gtfs_rt_url in settings["transit_type"][transit_type]["gtfs-rt_urls"]
    ]

    entities = []
    processing_times = {}
    with ThreadPoolExecutor() as executor:
        results = executor.map(fetch_gtfs_data, urls)
        for url, (content, elapsed_time) in zip(urls, results):
            processing_times[url] = elapsed_time
            feed = gtfs_realtime_pb2.FeedMessage()
            feed.ParseFromString(content)
            for entity in feed.entity:
                entity = clean_entity(stop_ids, entity)
                if len(entity.trip_update.stop_time_update) > 0:
                    entities.append(entity)

    upcoming_stops(settings["stops_to_return"], entities)
    return entities, processing_times

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
    ''' Get Arrival Data'''

    stop_id = update.stop_id
    route_id = f"^{re.escape(entity.trip_update.trip.route_id)}$"

    stop_name = gtfs_lookup(gtfs_static_data["stops"], "stop_id", f"^{stop_id}$")
    stop_name = ' + '.join({_["stop_name"] for _ in stop_name})

    route_short_name = gtfs_lookup(gtfs_static_data["routes"],
                                   "route_id",
                                   route_id)
    route_short_name = ' + '.join({_["route_short_name"] for _ in route_short_name})

    trip_headsign = gtfs_lookup(gtfs_static_data["trips"],
                                "trip_id",
                                entity.trip_update.trip.trip_id)
    trip_headsign = ' + '.join({_["trip_headsign"] for _ in trip_headsign})

    trip_direction = gtfs_lookup(gtfs_static_data["trips"],
                                "trip_id",
                                f".*{entity.trip_update.trip.trip_id}")

    stop = settings["transit_type"][transit_type]["stops"][stop_name]

    if "direction_id_to_name" in stop:
        trip_direction = stop["direction_id_to_name"].get(trip_direction[0]["direction_id"],
                                                          "Unknown Direction")

    else:
        if trip_direction:
            trip_direction = settings["direction_id_to_name"].get(trip_direction[0]["direction_id"],
                                                                  "Unknown Direction")
        else:
            trip_direction = "Unknown Direction"

    route_color= gtfs_lookup(gtfs_static_data['routes'],'route_id', route_id)
    route_color = f"#{route_color[0]['route_color']}"

    route_text_color = gtfs_lookup(gtfs_static_data['routes'],'route_id', route_id)
    route_text_color = f"#{route_text_color[0]['route_text_color']}"

    return {
            "stop_name": stop_name,
            "route_name": f"{route_short_name} {trip_direction}",
            "arrival_time": naturaltime(datetime.datetime.fromtimestamp((update.arrival.time))),
            "arrival_time_seconds": update.arrival.time - time.time(),
            "route_color": route_color,
            "route_text_color": route_text_color,
            "trip_direction": trip_direction,
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
    ''' group_and_filter_arrivals '''

    grouped_data = defaultdict(list)
    for arrival in arrivals:
        grouped_data[arrival["stop_name"]].append(arrival)

    filtered_data = []
    for _, group in grouped_data.items():
        filtered_data.extend(sorted(group, key=lambda x: x["arrival_time_seconds"])
                            [:stops_to_return])

    return filtered_data

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
def display_starting_point(starting_point):
    ''' starting_point '''
    debug_times = {}
    start_time = time.time()

    settings, debug_times["load_settings"] = time_function(load_settings, starting_point)
    transit_types = request.args.getlist('transit_type')

    if not transit_types:
        transit_types = list(settings["transit_type"].keys())

    my_datetime = datetime.datetime.now().astimezone(pytz.timezone(settings["timezone"]))
    arrivals = []
    processing_times = {}

    if is_quiet_time(settings):
        return render_template("quiet_time.html",
                               last_updated=my_datetime.strftime("%Y-%m-%d %I:%M:%S %p"))

    for transit_type in transit_types:
        if transit_type in settings["transit_type"]:
            gtfs_static_data_folder = settings["transit_type"][transit_type]["gtfs_static_data"]
            gtfs_static_data, debug_times[f"get_gtfs_static_data_{transit_type}"] = time_function(get_gtfs_static_data, gtfs_static_data_folder)
            entities, times = get_stops(settings, gtfs_static_data, transit_type)
            processing_times.update(times)

            for entity in entities:
                if 'trip_update' in entity:
                    for update in entity.trip_update.stop_time_update:
                        arrival_time = update.arrival.time

                        if arrival_time >= time.time():
                            arrival_data, debug_times[f"get_arrival_data_{transit_type}"] = time_function(get_arrival_data, settings, transit_type, gtfs_static_data, entity, update)
                            arrivals.append(arrival_data)

    filtered_arrivals, debug_times["group_and_filter_arrivals"] = time_function(group_and_filter_arrivals, arrivals, settings["stops_to_return"])
    arrivals = sorted(filtered_arrivals, key=itemgetter("arrival_time_seconds", "stop_name"))

    elapsed_time = time.time() - start_time

    return render_template("starting_point.html",
                           arrivals=arrivals,
                           last_updated=my_datetime.strftime("%Y-%m-%d %I:%M:%S %p"),
                           elapsed_time=round(elapsed_time, 2),
                           processing_times=processing_times,
                           debug_times=debug_times)

# Run the app
if __name__ == '__main__':
    app.run(debug=True)
