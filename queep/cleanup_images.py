"""Run as a private Cloud Run Job; no public maintenance endpoint."""
from queep.config import Settings, database, image_bucket
from queep.images import Images

if __name__ == "__main__":
    Settings()
    print(f"Cleaned image reservations: {Images(database(), image_bucket()).sweep()}")
