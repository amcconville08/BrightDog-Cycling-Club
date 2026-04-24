"""
poller.py - Background thread that cycles through all Strava-connected users,
fetches their activities, computes metrics, and updates the DB cache.
"""
import threading
import time
import logging
from datetime import datetime, timezone

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

        log.info("Poll cycle starting at %s", datetime.now(timezone.utc).isoformat())
        users = db.get_users_with_strava(self.db_path)
        log.info("Found %d Strava-connected users to poll", len(users))

        for user in users:
            try:
                token = strava.get_valid_token(
                    self.db_path, user["id"], self.client_id, self.client_secret
                )
                activities = strava.fetch_activities(token)
                ftp = user["ftp"] or 200

                # Load Garmin-seeded CTL baseline (if available) so CTL is grounded
                # in the full historical training record rather than just Strava history.
                ctl_seed = db.get_ctl_seed(self.db_path, user["id"])

                values = metrics.compute_metrics(activities, ftp, ctl_seed=ctl_seed)
                db.save_metrics_cache(self.db_path, user["id"], values)

                # Persist individual activities to activity_log for mcp-coach context
                db.log_activities(self.db_path, user["id"], activities, ftp)

                # Coaching
                state = coach_mod.TrainingState.from_metrics(values)
                result = coach_mod.evaluate(state)
                result_dict = result.to_dict()

                # Weekly outlook — deterministic, no LLM
                profile       = db.get_training_profile(self.db_path, user["id"])
                goal          = (profile.get("goal_custom") or profile.get("goal") or "") if profile else ""
                long_ride_day = profile.get("preferred_days", "") if profile else ""
                result_dict["weekly_outlook"] = coach_mod.generate_weekly_outlook(
                    state, goal, long_ride_day
                )

                db.save_coaching_cache(self.db_path, user["id"], result_dict)

                log.info(
                    "Polled user %s: CTL=%.1f ATL=%.1f TSB=%.1f (seed=%s)",
                    user["name"],
                    values.get("cycling_ctl", 0),
                    values.get("cycling_atl", 0),
                    values.get("cycling_tsb", 0),
                    "garmin" if ctl_seed else "none",
                )

                # Rate-limit spacing between users
                time.sleep(2)

            except Exception as exc:
                log.error("Poll failed for user %s: %s", user.get("name", "?"), exc)
