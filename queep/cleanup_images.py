"""Run as a private Cloud Run Job; no public maintenance endpoint."""
from queep.accounts import Accounts
from queep.config import Settings, database, image_bucket
from queep.images import Images

if __name__ == "__main__":
    Settings()
    print(f"Cleaned image reservations: {Images(database(), image_bucket()).sweep()}")
    completed = Accounts(database(), image_bucket(), Settings()).sweep()
    print(f"Completed account deletions: {completed}")
