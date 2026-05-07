"""
youtube_upload.py — Upload a video to YouTube.

Usage:
  python3 youtube_upload.py video.mp4 --title "My Video" --description "..." --thumbnail thumb.jpg
  python3 youtube_upload.py video.mp4 --title "My Video" --privacy public --tags "tag1,tag2"

Requires yt_token.json (run youtube_auth.py once to generate it).
"""

import argparse
import json
import os
import sys

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# Credentials dir: set YOUTUBE_CREDS_DIR env var, or defaults to /workspace/extra/youtube
# when running in a container, or projects/youtube relative to nanoclaw root on host.
_CREDS_DIR = os.environ.get(
    "YOUTUBE_CREDS_DIR",
    "/workspace/extra/youtube" if os.path.isdir("/workspace") else
    os.path.join(os.path.dirname(__file__), "..", "projects", "youtube")
)
SECRETS_FILE = os.path.join(_CREDS_DIR, "yt_client_secrets.json")
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def load_credentials(channel: int = 2):
    token_file = os.path.join(_CREDS_DIR, "yt_token.json" if channel == 1 else f"yt_token_{channel}.json")
    if not os.path.exists(token_file):
        print(f"ERROR: token not found at {token_file}")
        print(f"Run: python3 scripts/youtube_auth.py --out {os.path.basename(token_file)}")
        sys.exit(1)

    with open(token_file) as f:
        data = json.load(f)

    creds = Credentials(
        token=data["token"],
        refresh_token=data["refresh_token"],
        token_uri=data["token_uri"],
        client_id=data["client_id"],
        client_secret=data["client_secret"],
        scopes=data["scopes"],
    )

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        data["token"] = creds.token
        with open(token_file, "w") as f:
            json.dump(data, f, indent=2)

    return creds


def upload(args):
    if not os.path.exists(args.video):
        print(f"ERROR: video file not found: {args.video}")
        sys.exit(1)

    creds = load_credentials(args.channel)
    youtube = build("youtube", "v3", credentials=creds)

    tags = [t.strip() for t in args.tags.split(",")] if args.tags else []

    body = {
        "snippet": {
            "title": args.title,
            "description": args.description,
            "tags": tags,
            "categoryId": str(args.category),
        },
        "status": {
            "privacyStatus": args.privacy,
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": False,
        },
    }

    media = MediaFileUpload(args.video, mimetype="video/mp4", resumable=True, chunksize=4 * 1024 * 1024)

    print(f"Uploading: {args.video}")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            pct = int(status.progress() * 100)
            print(f"  {pct}%", end="\r", flush=True)

    video_id = response["id"]
    print(f"\nUploaded: https://www.youtube.com/watch?v={video_id}")

    if args.thumbnail:
        if not os.path.exists(args.thumbnail):
            print(f"WARNING: thumbnail not found: {args.thumbnail} — skipping")
        else:
            ext = os.path.splitext(args.thumbnail)[1].lower()
            mime = "image/png" if ext == ".png" else "image/jpeg"
            youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(args.thumbnail, mimetype=mime),
            ).execute()
            print(f"Thumbnail set: {args.thumbnail}")

    return video_id


def main():
    parser = argparse.ArgumentParser(description="Upload a video to YouTube")
    parser.add_argument("video", help="Path to .mp4 file")
    parser.add_argument("--title", required=True, help="Video title")
    parser.add_argument("--description", default="", help="Video description")
    parser.add_argument("--thumbnail", default=None, help="Path to thumbnail (jpg or png)")
    parser.add_argument("--privacy", default="unlisted", choices=["public", "unlisted", "private"])
    parser.add_argument("--category", default=22, type=int, help="YouTube category ID (default 22 = People & Blogs)")
    parser.add_argument("--tags", default="", help="Comma-separated tags")
    parser.add_argument("--channel", default=2, type=int, help="Channel number (1=Game Vibe Feels, 2=Velikov's Visions [default])")
    args = parser.parse_args()
    upload(args)


if __name__ == "__main__":
    main()
