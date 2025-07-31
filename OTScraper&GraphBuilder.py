import requests
import time
from collections import defaultdict
import concurrent.futures
import threading
from tqdm import tqdm
import json


ClientID = '' #you need to get Oauth from osu! insert ur client ID
ClientSecret = '' #insert your client secret
ForumID = 52  # This is the OT forum ID
MaxTopics = 200
TimeoutThread = 10  #time it will take intill thread timeout (fixes problem but I dont understand why it happens)
RequestTimout = 10



UsernameCache = {} # this and the banned user cache is to avoid useless api requests
UserCacheLock = threading.Lock()

BannedUserCache = set()
BannedCacheLock = threading.Lock()

ShouldContiune = True
ShouldContiuneLock = threading.Lock()


def get_token():
    try:
        res = requests.post(
            "https://osu.ppy.sh/oauth/token",
            json={
                "client_id": ClientID,
                "client_secret": ClientSecret,
                "grant_type": "client_credentials",
                "scope": "public"
            },
            timeout=RequestTimout
        )
        res.raise_for_status()
        return res.json()["access_token"]
    except (requests.exceptions.RequestException, KeyError) as e:
        print(f"Error getting token: {e}")
        raise


def should_process_continue():
    with ShouldContiuneLock:
        return ShouldContiune


def stop_processing():
    global ShouldContiune
    with ShouldContiuneLock:
        ShouldContiune = False


def save_graph_to_json(graph, filename="interactionsWithTimestamps.json"):
    serializable_graph = {
        str(k): {str(inner_k): v for inner_k, v in v_dict.items()}
        for k, v_dict in graph.items()
    }

    with open(filename, "w") as f:
        json.dump(serializable_graph, f)


def get_username(token, UserID):
    if not should_process_continue():
        raise Exception("processing timeout")


    with BannedCacheLock:
        if UserID in BannedUserCache:
            return None


    with UserCacheLock:
        if UserID in UsernameCache:
            return UsernameCache[UserID]

    try:
        res = requests.get(
            f"https://osu.ppy.sh/api/v2/users/{UserID}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=RequestTimout
        )
        if res.status_code == 404: # if we dont find or get the user we assume its banned
            with BannedCacheLock:
                BannedUserCache.add(UserID)
            return None

        res.raise_for_status()
        username = res.json().get("username")
        if username:
            with UserCacheLock:
                UsernameCache[UserID] = username
            return username
        return str(UserID)
    except requests.exceptions.RequestException as e:
        print(f"Error getting username for user {UserID}: {e}")
        return str(UserID)


def get_recent_topics(token, limit=MaxTopics, batch_size=50): #im pretty sure maxbatch size within api limits is 50
    """
    This function gets request threads from OT in batches (50 threads per batch)
    probably one of the smarter optimizations i've made (thank god osu!api has pagination)
    """

    AllTopics = []
    cursor = None

    with tqdm(total=limit, desc="Fetching topics") as pbar:
        while len(AllTopics) < limit:
            try:
                current_limit = min(batch_size, limit - len(AllTopics))
                params = {
                    "forum_id": ForumID,
                    "limit": current_limit,
                    "sort": "new"
                }
                if cursor:
                    params["cursor_string"] = cursor

                res = requests.get(
                    "https://osu.ppy.sh/api/v2/forums/topics",
                    headers={"Authorization": f"Bearer {token}"},
                    params=params,
                    timeout=RequestTimout
                )
                res.raise_for_status()
                data = res.json()

                newTopics = data.get("topics", [])
                AllTopics.extend(newTopics)
                pbar.update(len(newTopics))

                cursor = data.get("cursor_string")
                if not newTopics or not cursor:
                    break

                time.sleep(1)

            except requests.exceptions.RequestException as e:
                print(f"Error fetching topic batch: {e}")
                break

    return AllTopics[:limit]


def get_topic_posts(token, topic_id):
    """
    Get the acutall thread/post this will hit API rate limmit i need to implement some sort of
    smart delay
    """
    AllPosts = []
    cursor = None
    start_time = time.time()

    while True:
        if time.time() - start_time > TimeoutThread:
            print(f"Timeout {topic_id} ingnoring")
            return None

        params = {
            "limit": 50,
            "sort": "id_asc"
        }
        if cursor:
            params["cursor_string"] = cursor

        try:
            res = requests.get(
                f"https://osu.ppy.sh/api/v2/forums/topics/{topic_id}",
                headers={"Authorization": f"Bearer {token}"},
                params=params,
                timeout=min(10, RequestTimout)
            )
            res.raise_for_status()
            data = res.json()
        except requests.exceptions.RequestException as e:
            print(f"Error getting posts for topic {topic_id}: {e}")
            return None

        posts = data.get("posts", [])
        AllPosts.extend(posts)

        cursor = data.get("cursor_string")
        if not cursor or len(posts) < 50:
            break

        time.sleep(0.1)

    return AllPosts


def process_topic(token, topic, graph):
    topic_id = topic["id"]
    try:
        posts = get_topic_posts(token, topic_id)
        if posts is None or not posts:
            return

        starter_id = posts[0].get("user_id")
        if not starter_id:
            return

        starter_username = get_username(token, starter_id)
        if not starter_username:
            return

        for post in posts[1:]:
            replier_id = post.get("user_id")
            if not replier_id:
                continue

            replier_username = get_username(token, replier_id)
            if not replier_username:
                continue

            post_body = post.get("body", {}).get("content", "")
            timestamp = post.get("created_at")
            if not timestamp:
                continue

            # This is to check for codes in posts (Currently not sutiable) WIP
            quoted_usernames = set()
            if "[quote=" in post_body.lower():
                import re
                quote_matches = re.findall(r'\[quote="([^"]+)"\]', post_body, re.IGNORECASE)
                quoted_usernames.update(match.strip() for match in quote_matches)


            for quoted_username in quoted_usernames:
                if quoted_username and quoted_username != replier_username:
                    with threading.Lock():
                        graph[replier_username][quoted_username].append(timestamp)
                        graph[quoted_username][replier_username].append(timestamp)


            if replier_username != starter_username:
                with threading.Lock():
                    graph[replier_username][starter_username].append(timestamp)
                    graph[starter_username][replier_username].append(timestamp)

    except Exception as e:
        print(f"Error in topic {topic_id} {e}")


def build_interaction_graph(token, topics):
    """
    Actually maps users interactions one to another and creates the graph ADT
    """
    graph = defaultdict(lambda: defaultdict(list))
    completed_topics = 0

    with tqdm(total=len(topics), desc="Processing topics") as pbar:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = {
                executor.submit(process_topic, token, topic, graph): topic["id"]
                for topic in topics
            }

            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()
                    completed_topics += 1
                    pbar.update(1)
                except Exception as e:
                    topic_id = futures[future]
                    print(f"Topic {topic_id} failed: {str(e)}")
                    pbar.update(1)

    print(f"\nCompleted {completed_topics}/{len(topics)} topics successfully")
    return graph


def main():
    try:
        token = get_token()
        topics = get_recent_topics(token)

        if not topics:
            print("bruh")
            return
        """
        PLease appreciate the effort I put into these messages
        """
        print(f"Building graph from {len(topics)} topics")
        start_time = time.time()
        graph = build_interaction_graph(token, topics)
        print(f"Graph built took {time.time() - start_time:.2f} seconds")
        save_graph_to_json(graph)
        print("\nSample interactions:")
        for user1, interactions in list(graph.items())[:5]:
            for user2, timestamps in list(interactions.items())[:5]:
                print(f"{user1} : {user2}: {len(timestamps)} interactions latest at {timestamps[-1]}")

        print("\nUsername cache stats:")
        print(f"Stored {len(UsernameCache)} usernames")
        print(f"Banned user cache size: {len(BannedUserCache)}")
    except Exception as e:
        print(f"Fatal error in main: {e}")


if __name__ == "__main__":
    main()
