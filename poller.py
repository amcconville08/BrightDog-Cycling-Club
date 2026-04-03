"""
poller.py - Background thread that cycles through all Strava-connected users,
fetches their activities, computes metrics, and updates the DB cache.
"""
import threading
import time
import logging
from datetime import datetime, timezone, date, timedelta

log = logging.getLogger("cycling-club.poller")


class Poller(threading.Thread):
    daemon = True

    def __init__(self, db_path: str, client_id: str, client_secret: str, interval: int = 600):
        super().__init__(name="poller")
        self.db_path = db_path
        self.client_id = client_id
        self.client_secret = client_secret
        self.interval = interval

    def run(self):
        time.sleep(5)  # let Flask start
        while True:
            self._poll_all()
            time.sleep(self.interval)

    def _poll_all(self):
        import db
        import strava
        import metrics
        import coaching as coach_mod
        import reconciliation as recon_mod

        log.info("Poll cycle starting at %s", datetime.now(timezone.utc).isoformat())
        users = db.get_users_with_strava(self.db_path)
        log.info("Found %d Strava-connected users to poll", len(users))

        today_str = date.today().isoformat()
        yesterday_str = (date.today() - timedelta(days=1)).isoformat()

        for user in users:
            try:
                user_id = user["id"]

                token = strava.get_valid_token(
                    self.db_path, user_id, self.client_id, self.client_secret
                )
                activities = strava.fetch_activities(token)

                # Use FTP from history (most recent), fall back to users.ftp
                ftp = db.get_current_ftp(self.db_path, user_id, fallback=float(user["ftp"] or 200))

                # Persist recent rides to activity_log for reconciliation
                db.log_activities(self.db_path, user_id, activities, ftp)

                values = metrics.compute_metrics(activities, ftp)
                db.save_metrics_cache(self.db_path, user_id, values)

                # Load training profile and today's planned workout for coaching context
                training_profile = db.get_training_profile(self.db_path, user_id)
                planned_workout = db.get_today_workout(self.db_path, user_id, today_str)
                pw_dict = dict(planned_workout) if planned_workout else None

                # Yesterday's plan reconciliation
                yesterday_plan = db.get_today_workout(self.db_path, user_id, yesterday_str)
                yesterday_rides = db.get_activities_for_date(self.db_path, user_id, yesterday_str)
                yesterday_pw_dict = dict(yesterday_plan) if yesterday_plan else None
                yesterday_recon = recon_mod.evaluate_plan_reconciliation(
                    yesterday_pw_dict, yesterday_rides
                )

                state = coach_mod.TrainingState.from_metrics(values)
                result = coach_mod.evaluate(state, training_profile=training_profile, planned_workout=pw_dict)

                # Recent rides for pattern analysis
                recent_rides = db.get_recent_activities(self.db_path, user_id, days=14)

                # Ride style suggestion
                suggestion = recon_mod.suggest_ride(
                    classification=result.classification,
                    tsb=values.get("cycling_tsb", 0),
                    weekly_hours=values.get("cycling_rolling_7d_moving_time_seconds", 0) / 3600.0,
                    weekly_tss=values.get("cycling_weekly_tss", 0),
                    training_profile=training_profile,
                    yesterday_recon=yesterday_recon,
                    today_plan=pw_dict,
                    recent_rides=recent_rides,
                )

                coaching_dict = result.to_dict()
                coaching_dict.update(suggestion)
                coaching_dict.update(yesterday_recon.to_dict())
                db.save_coaching_cache(self.db_path, user_id, coaching_dict)

                log.info(
                    "Polled user %s: CTL=%.1f ATL=%.1f TSB=%.1f class=%s style=%s",
                    user["name"],
                    values.get("cycling_ctl", 0),
                    values.get("cycling_atl", 0),
                    values.get("cycling_tsb", 0),
                    result.classification,
                    suggestion.get("ride_style", "?"),
                )

                # Rate-limit spacing between users
                time.sleep(2)

            except Exception as exc:
                log.error("Poll failed for user %s: %s", user.get("name", "?"), exc, exc_info=True)
