import psycopg2
import pandas as pd
import numpy as np
import itertools as it
from io import StringIO
import boto3
import os

# Define psql config
HOST = os.environ.get('DB_HOST') or "st-deploy-ds-apps-db.cypzti2esilk.us-east-1.rds.amazonaws.com"
DB_NAME = os.environ.get('DB_NAME') or "stdemo"
USER = os.environ.get('DB_USER') or "odsc"
PASSWORD = os.environ.get('DB_PASSWORD') or "password"

# Define date ranges for analysis, we won't actually use this but this would be set to limit the engagements
# that we are analyzing
LAST_DATE = "yesterday"
CURRENT_TIME = "today"

# Define global config S3  output
S3_BUCKET = 'st-batch-demo'
S3_PATH = 'cron/room/today'
RES_FILE_NAME = "room_recs.csv"

# Define Runtime config
N_CLOSEST = 10
N_REC = 5

# define some helper functions

def agg_closest(x):
    return ','.join([str(user) for user in x.head(N_CLOSEST).values])

def agg_weight(x):
    return ','.join(['{:.2f}'.format(w) for w in x.head(N_CLOSEST).values])

def agg_reqs(x):
    return ','.join([str(user) for user in x.head(N_REC).values])

def agg_reqs_weight(x):
    return ','.join(['{:.2f}'.format(w) for w in x.head(N_REC).values])

def write_to_s3(content, filename, bucket, path):
    # define S3 connection in function call to avoid timeout
    csv_buffer = StringIO()
    content.to_csv(csv_buffer, index=False)
    s3_resource = boto3.resource('s3')
    s3_resource.Object(bucket, os.path.join(S3_PATH, filename)).put(Body=csv_buffer.getvalue())

# open psql connections
try: 
    conn = psycopg2.connect(f"host={HOST} dbname={DB_NAME} user={USER} password={PASSWORD}")
except psycopg2.Error as e: 
    print("Error: Could not make connection to the Postgres database")
    print(e)

# read in data from psql
messages = pd.read_sql_query("SELECT id, user_id, room_id, mention_ids FROM messages", conn, coerce_float=False)
likes = pd.read_sql_query("SELECT user_id, message_id FROM likes", conn, coerce_float=False)
follows = pd.read_sql_query("SELECT user_id, following_user_id FROM follows", conn, coerce_float=False)
subscriptions = pd.read_sql_query("SELECT user_id, room_id FROM subscriptions", conn, coerce_float=False)

# first we will get first degree user engagments to find who we are closest too
# we need to get all user > user engagements so we need to "unstack" the mention_ids
mentions = []
for message, user, room, mention_ids in messages.values:
    if not mentions:
        continue
    msg_mentions = [int(mention_id) for mention_id in mention_ids.split(",")]
    for mention in msg_mentions:
        mentions.append([user, mention])

mentions = pd.DataFrame(mentions, columns=["user_id", "mentioned_user_id"])

# we need to joing likes and messages to get the user that posted the message that was liked
liked_users = likes.merge(messages, how='left', left_on="message_id", right_on="id", suffixes=("", "_liked"))
liked_users = liked_users.loc[:, ("user_id", "user_id_liked")]

# merge all user to user engagements into a single tables
# first update all of the columne names to be uniform
follows.rename(columns={"following_user_id": "engaged_with_user_id"}, inplace=True)
mentions.rename(columns={"mentioned_user_id": "engaged_with_user_id"}, inplace=True)
liked_users.rename(columns={"user_id_liked": "engaged_with_user_id"}, inplace=True)

user_engagements = pd.concat([follows, mentions, liked_users], axis=0)
# add a weight of 1 per engagement
user_engagements.loc[:, 'weight'] = 1

# now we want to count the number of user engagements for each pair to get a weight and take the top N
user_eng_count = user_engagements.groupby(["user_id", "engaged_with_user_id"]).agg({'weight': sum}).reset_index()
# now we want to take the N_CLOSEST engaged with users and their corresponding weights
user_eng_count.sort_values(['weight', 'engaged_with_user_id'], ascending=[False, True], inplace=True)
closest_connections = user_eng_count.groupby('user_id').agg({'engaged_with_user_id': agg_closest, 'weight': agg_weight}).reset_index()

# we need to unstack the closest connections into unique records of "first degree connections"
closest_connections.loc[:, "engaged_with_user_id"] = closest_connections.loc[:, "engaged_with_user_id"].apply(lambda x: [int(u) for u in x.split(",")])
closest_connections.loc[:, "weight"] = closest_connections.loc[:, "weight"].apply(lambda x: [float(w) for w in x.split(",")])
first_degree_user_cnx = pd.DataFrame({
            "user_id": np.repeat(closest_connections['user_id'].values, closest_connections['engaged_with_user_id'].str.len()),
            "engaged_with_user_id": list(it.chain.from_iterable(closest_connections['engaged_with_user_id'])),
            "weight": list(it.chain.from_iterable(closest_connections['weight']))
        })

# now we need to calculate weights for first degree room engagements
# we need to just pull out the rooms that people have messaged in
messaged_in_rooms = messages.loc[:, ("user_id", "room_id")].copy()

# we need to joing likes and messages to get the room the message that was liked was posted in
liked_rooms = likes.merge(messages, how='left', left_on="message_id", right_on="id", suffixes=("", "_liked"))
liked_rooms = liked_rooms.loc[:, ("user_id", "room_id")]

room_engagements = pd.concat([subscriptions, messaged_in_rooms, liked_rooms], axis=0)
room_engagements.rename(columns={"room_id": "engaged_with_room_id"}, inplace=True)
# add a weight of 1 per engagement
room_engagements.loc[:, 'weight'] = 1

# now we want to count the number of room engagements for each pair to get a weight and take the top N
room_eng_count = room_engagements.groupby(["user_id", "engaged_with_room_id"]).agg({'weight': sum}).reset_index()
# now we want to take the N_CLOSEST engaged with users and their corresponding weights
room_eng_count.sort_values(['weight', 'engaged_with_room_id'], ascending=[False, True], inplace=True)
closest_rooms = room_eng_count.groupby('user_id').agg({'engaged_with_room_id': agg_closest, 'weight': agg_weight}).reset_index()

# we need to unstack the closest rooms into unique records of "first degree connections"
closest_rooms.loc[:, "engaged_with_room_id"] = closest_rooms.loc[:, "engaged_with_room_id"].apply(lambda x: [int(u) for u in x.split(",")])
closest_rooms.loc[:, "weight"] = closest_rooms.loc[:, "weight"].apply(lambda x: [float(w) for w in x.split(",")])
first_degree_room_cnx = pd.DataFrame({
            "user_id": np.repeat(closest_rooms['user_id'].values, closest_rooms['engaged_with_room_id'].str.len()),
            "engaged_with_room_id": list(it.chain.from_iterable(closest_rooms['engaged_with_room_id'])),
            "weight": list(it.chain.from_iterable(closest_rooms['weight']))
        })

# now we merge first degree user connections and first degree room cnx to get second degree room cnx
second_degree_room_cnx = first_degree_user_cnx.merge(first_degree_room_cnx, 
                                                    left_on="engaged_with_user_id",
                                                    right_on="user_id",
                                                    suffixes=("", "_sec_deg"))
# drop copy of engaged_with_user_id (user_id_sec_deg) and first degree weight
second_degree_room_cnx.drop(["user_id_sec_deg", "weight"], inplace=True, axis=1)

# remove 2nd degree rooms that you already subscribe to
temp = pd.merge(second_degree_room_cnx, subscriptions, 
                left_on=["user_id", "engaged_with_room_id"],
                right_on=["user_id", "room_id"],
                how="left", 
                indicator=True, 
                suffixes=("", "_subscribe"))

second_degree_room_cnx_valid = temp[temp["_merge"] == "left_only"].copy()
# drop unnecesary fields
second_degree_room_cnx_valid.drop(["_merge", "room_id"], inplace=True, axis=1)

# now we want to aggregate by second degree connection engagement, we will also collect the intermediate connections
# that lead to this rec
second_degree_room_eng_count = second_degree_room_cnx_valid.groupby(["user_id", "engaged_with_room_id"]).agg({"weight_sec_deg": sum, "engaged_with_user_id": agg_closest}).reset_index()

# sort agg weights and group by user_id to get recs
second_degree_room_eng_count.sort_values(["weight_sec_deg", "user_id"], ascending=[False, True], inplace=True)
recs = second_degree_room_eng_count.groupby("user_id").agg({"engaged_with_room_id": agg_reqs,
                                                        "weight_sec_deg": agg_reqs_weight,
                                                        "engaged_with_user_id": lambda x: ";".join(tuple(x)[:N_REC])}).reset_index()

# rename colums for recs         
recs.rename(columns={"engaged_with_room_id": "recommendations",
                     "weight_sec_deg": "recommendation_weights",
                     "engaged_with_user_id": "recommendation_reasons"},
            inplace=True)

write_to_s3(recs, RES_FILE_NAME, S3_BUCKET, S3_PATH)   
