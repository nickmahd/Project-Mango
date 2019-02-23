#!/usr/bin/env python3
import time
import os
import sys
import logging

import praw
import prawcore
import pandas as pd

from stopwatch import Stopwatch
from handler import Handler
import config


attr = config.ATTR + config.S_ATTR + ['time_now'] + ['pickup_no'] + ['post_pickup']
p_attr = config.ATTR
s_attr = config.S_ATTR

FORMAT = '%(filename)s | %(asctime)s.%(msecs)03d %(levelname)s @ %(lineno)d: %(message)s'
DATEFMT = '%Y-%m-%d %H:%M:%S'

logger = logging.getLogger(__name__)
handler = logging.FileHandler(config.LOGFILE)
formatter = logging.Formatter(FORMAT, datefmt=DATEFMT)
logger.setLevel('INFO')
handler.setLevel('INFO')
handler.setFormatter(formatter)
logger.addHandler(handler)


def get_error():
    global e_type, e_obj, e_tb, tb
    e_type, e_obj, e_tb = sys.exc_info()
    tb = (f'{e_type.__name__} @ {e_tb.tb_lineno}: \"{e_obj}\"')

    return tb


def auth():
    reddit = praw.Reddit(client_id=config.CLIENT_ID,
                    client_secret=config.CLIENT_SECRET,
                    password=config.PASSWORD,
                    username=config.USERNAME,
                    user_agent=config.USER_AGENT)

    return reddit


def main():
    logger.info(f"-- {time.strftime('%Y-%m-%d %H:%M:%S')} on {sys.platform}, pid {os.getpid()}")
    logger.info(f"-- Reading from {config.SUBREDDIT}; for more inforation see config.py.")

    r = auth()
    s = r.subreddit(config.SUBREDDIT)
    
    df = pd.DataFrame(columns=attr)
    handler = Handler()
    stopwatch = Stopwatch()

    def kill_check():
        if handler.killed:
            logger.info(f"Received kill signal {handler.lastSignal} (code {handler.lastSignum})")
            if not config.DRY_RUN:
                logger.info("Writing dataframe to .CSV")
                try:
                    df.drop(['pickup_no', 'post_pickup'], axis=1).to_csv(config.DATAFILE, index=False)
                except Exception:
                    logger.warning(get_error())
                    logger.warning("Failed to write to CSV.")
                else:
                    logger.info("Successfully wrote dataframe.")
            logger.info("Exited.")
    
            return True

        else:
            return False

    retries = 0

    while not handler.killed:
        try:
            if retries:
                logger.info(f"Attempting to retry, attempt {retries}...")

            values = df.sort_values('pickup_no', ascending=False).drop_duplicates(subset=['id']).sort_index().reset_index(drop=True)

            row = dict((a, []) for a in attr)

            logger.info(f'{len(values)} unique values')
    
            # There are better ways of doing this entire block. Also it might be slow
            for post_id in values['id'].values:
                stopwatch.reset()

                match_row = values.loc[values['id'] == post_id]
                iteration = match_row['pickup_no'].iloc[0]
                pickup = match_row['post_pickup'].iloc[0]

                logger.info(f"{post_id}: queued for {(time.time() - pickup)} / {(config.POST_PICKUPS[iteration])} secs")
                logger.info(f"p# is {match_row['pickup_no'].iloc[0]} / {len(config.POST_PICKUPS)}")
 
                if iteration == len(config.POST_PICKUPS):
                    logger.info("Hit final iteration, dropping")
                    continue

                if (time.time() - pickup) < config.POST_PICKUPS[iteration]:
                    continue

                post = r.submission(post_id)
                for _a in p_attr:
                    row[_a].append(getattr(post, _a))
                for _s in s_attr:
                    row[_s].append(getattr(s, _s))
                row['time_now'].append(time.time())
                row['pickup_no'].append(iteration + 1)
                row['post_pickup'].append(pickup)

                # MAGIC NUMBER 2.5: don't know just threw it in there
                # it's a good estimate for how long it should take to get a post
                if stopwatch.mark() > 2.5 * len(values.index):
                    logger.warning(f'Warning: Slow iteration, {stopwatch.mark()} secs for {len(values.index)} items')

            row_new = dict((a, []) for a in attr)

            for post in s.new(limit=config.POST_GET_LIMIT):
                if post.id in df['id'].values:
                    logger.info(f"{post.id} is a duplicate, continuing")
                    continue

                logger.info(f"Picked up {post.id}")
                
                for _a in p_attr:
                    row_new[_a].append(getattr(post, _a))
                for _s in s_attr:
                    row_new[_s].append(getattr(s, _s))
                row_new['time_now'].append(time.time())
                row_new['pickup_no'].append(0)
                row_new['post_pickup'].append(time.time())

            logger.info(f"Old row has {len(row['id'])}")
            logger.info(f"New row has {len(row_new['id'])}")

            df_new = pd.DataFrame(row_new, columns=attr)
            df_update = pd.DataFrame(row, columns=attr)

            if df.equals(df.append(df_new)) and df.equals(df.append(df_update)):
                modified = False
            else:
                modified = True
                df = pd.concat([df, df_new, df_update], ignore_index=True)
                if not config.DRY_RUN:
                    df.drop(['pickup_no', 'post_pickup'], axis=1).to_csv(config.DATAFILE, index=False)

            logger.info(len(df.index))

            del row
            del row_new
            del df_new
            del df_update

        except prawcore.exceptions.RequestException:  # You most likely do not need this
            retries += 1
            if retries < len(config.TIMEOUTS):
                logger.warning(f'Connection lost. Waiting for {config.TIMEOUTS[retries-1]} sec...')
                time.sleep(config.TIMEOUTS[retries-1])
            else:
                logger.critical('Max retries exceeded. Exiting.')
                break

        except Exception:  # Or this
            logger.error(get_error())
            os._exit(1)

        else:
            if retries:
                logger.info("Connection reestablished")
                retries = 0

            if modified:
                logger.info(f"{len(df.index)} entries, {len(df.drop_duplicates(subset=['id']).index)} unique")

        finally:
            rem = r.auth.limits['remaining']
            res = r.auth.limits['reset_timestamp'] - time.time()
            if rem < 5:
                logger.warning('Out of calls! Waiting...')
                for _ in range(int(res + 1)):
                    time.sleep(1)
                    if kill_check():
                        killed = True
                    else:
                        killed = False

                if (res - config.TIMEOUT_SECS) > 0 and not killed:
                    for _ in range(int(res - config.TIMEOUT_SECS)):
                        time.sleep(1)
                        if kill_check():
                            break
            else:
                if modified:
                    logger.info(f"{rem:.0f} calls remaining, {res:.0f} till next reset")
                for _ in range(config.TIMEOUT_SECS):
                    time.sleep(1)
                    if kill_check():
                        break

            # No, you do not have to 'if handler.killed: break', it's a while loop

if __name__ == "__main__":
    main()
