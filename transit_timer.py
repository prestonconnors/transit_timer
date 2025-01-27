''' transit_timer'''

import csv
import copy
import datetime
import os
import re
import time

from operator import itemgetter
from itertools import groupby

import pycron
import pytz
import requests
import yaml

from flask import Flask, render_template
from humanize import naturaltime
from google.transit import gtfs_realtime_pb2

# Initialize Flask app
templates_folder = os.path.join(os.path.dirname(os.path.realpath(__file__)), "templates")
app = Flask(__name__, template_folder=templates_folder)

def get_gtfs_static_data(gtfs_static_data):
    ''' Get static GTFS data '''
    data_dict = {}
    for file_name in os.listdir(gtfs_static_data):
        file_path = os.path.join(gtfs_static_data, file_name)
        if os.path.isfile(file_path):
            with open(file_path, encoding="utf-8") as file:
                data = list(csv.DictReader(file))
                data_dict[os.path.splitext(os.path.basename(file_path))[0]] = data

    return data_dict

def gtfs_lookup(data, column_name, match):
    '''Look up GTFS data'''
    matches = []
    for d_d in data:
        if re.search(match, d_d[column_name]):
        #if match == d_d[column_name]:
            matches.append(d_d)
    return matches

def get_stops(settings, gtfs_static_data, transit_type):
    ''' Get Stops'''
    if transit_type == "bus":
        with open(settings["bus_key_file"], "r", encoding="utf-8") as file:
            key = f"?key={file.read().strip()}"
    else:
        key = ""

    stop_ids = []
    stop_names = list(settings["transit_type"][transit_type]["stops"])
    for stop_name in stop_names:
        stop_ids += [_["stop_id"] for _ in gtfs_lookup(gtfs_static_data["stops"],
                                                       "stop_name",
                                                       stop_name)]

    for gtfs_rt_url in settings["transit_type"][transit_type]["gtfs-rt_urls"]:
        feed = gtfs_realtime_pb2.FeedMessage()
        url = f"{gtfs_rt_url}{key}"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        feed.ParseFromString(response.content)

        entities = []
        for entity in feed.entity:
            entity = clean_entity(stop_ids, entity)

            if len(list(entity.trip_update.stop_time_update)) > 0:
                entities.append(entity)

    upcoming_stops(settings["stops_to_return"], entities)
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
    '''Return the N upcoming stop updates'''

    stop_id = {}
    for entity in entities:
        for update in entity.trip_update.stop_time_update:
            if update.stop_id not in stop_id:
                stop_id[update.stop_id] = []
            stop_id[update.stop_id].append(update.arrival.time)

    for key, value in stop_id.items():
        stop_id[key] = sorted(value)[0:stops_to_return]

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

    # Lookup static GTFS data
    route_short_name = gtfs_lookup(gtfs_static_data["routes"],
                                   "route_id",
                                   f"^{entity.trip_update.trip.route_id}$")
    route_short_name = ' + '.join({_["route_short_name"] for _ in route_short_name})

    trip_headsign = gtfs_lookup(gtfs_static_data["trips"],
                                "trip_id",
                                entity.trip_update.trip.trip_id)
    trip_headsign = ' + '.join({_["trip_headsign"] for _ in trip_headsign})

    stop_name = gtfs_lookup(gtfs_static_data["stops"], "stop_id", f"^{stop_id}$")
    stop_name = ' + '.join({_["stop_name"] for _ in stop_name})

    background_color=settings["transit_type"][transit_type]["stops"][stop_name]["background_color"]

    return {
            "stop_name": stop_name,
            "route_name": f"{route_short_name} {trip_headsign}",
            "arrival_time": naturaltime(datetime.datetime.fromtimestamp((update.arrival.time))),
            "arrival_time_seconds": update.arrival.time - time.time(),
            "background_color": background_color,
    }

def display_stop(timezone, schedule):
    ''' display_stop '''

    # Convert to Eastern Time
    my_timezone = pytz.timezone(timezone)
    my_datetime = datetime.datetime.now().astimezone(my_timezone)

    return bool(pycron.is_now(schedule, my_datetime))

def is_quiet_time(settings):
    ''' is_quiet_time '''
    total_stops = 0
    deleted_stops = 0
    for transit_type in settings["transit_type"]:
        for stop in list(settings["transit_type"][transit_type]["stops"].keys()):
            if not display_stop("US/Eastern",
                                settings["transit_type"][transit_type]["stops"][stop]["schedule"]):
                del settings["transit_type"][transit_type]["stops"][stop]
                deleted_stops += 1
            total_stops += 1

    return bool(deleted_stops == total_stops)


@app.route('/starting_point/<starting_point>')
def index(starting_point):
    ''' index '''

    # Get starting_point parameter from the request
    settings = load_settings(starting_point)

    entities = {}
    arrivals = []

    if is_quiet_time(settings):
        return render_template("quiet_time.html")


    for transit_type in settings["transit_type"]:
        gtfs_static_data_folder = settings["transit_type"][transit_type]["gtfs_static_data"]
        gtfs_static_data = get_gtfs_static_data(gtfs_static_data_folder)
        entities = get_stops(settings, gtfs_static_data, transit_type)

        for entity in entities:
            if 'trip_update' in entity:
                for update in entity.trip_update.stop_time_update:
                    arrival_time = update.arrival.time

                    if arrival_time >= time.time():
                        arrivals.append(get_arrival_data(settings,
                                                         transit_type,
                                                         gtfs_static_data,
                                                         entity,
                                                         update))

    arrivals = sorted(arrivals, key=itemgetter("stop_name", "arrival_time_seconds"))
    # Group entries by 'stop_name'
    grouped_data = groupby(arrivals, key=itemgetter('stop_name'))

    # Create a new list that will store only the first 3 entries for each group
    filtered_data = []

    # Iterate over each group and add only the first 3 entries of each stop_name to the list
    for _, group in grouped_data:
        group_list = list(group)  # Convert the group to a list to work with
        filtered_data.extend(group_list[:3])  # Add only the first 3 entries

    # Replace the original list with the filtered list
    arrivals = sorted(filtered_data, key=itemgetter("stop_name", "arrival_time_seconds"))

    my_datetime = datetime.datetime.now().astimezone(pytz.timezone(settings["timezone"]))

    return render_template("index.html",
                           arrivals=arrivals,
                           last_updated=my_datetime.strftime("%Y-%m-%d %I:%M:%S %p"))

# Run the app
if __name__ == '__main__':
    app.run(debug=True)
