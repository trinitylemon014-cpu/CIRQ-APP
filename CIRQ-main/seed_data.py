"""Seed reference data (schools + their communities). Idempotent: safe to run more than once.

    python seed_data.py schools.txt

It never creates students, posts, followers or any engagement numbers.
"""
import re
import sys
from dotenv import load_dotenv
import os
from supabase import create_client

load_dotenv()


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def main(path):
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_SECRET_KEY")
    if not url or not key:
        sys.exit("Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (in .env) first.")
    db = create_client(url, key)
    names = [l.strip() for l in open(path, encoding="utf-8") if l.strip() and not l.startswith("#")]
    for name in names:
        slug = slugify(name)
        if db.table("schools").select("id").eq("slug", slug).execute().data:
            print("exists  ", name)
            continue
        school = db.table("schools").insert({"name": name, "slug": slug}).execute().data[0]
        db.table("communities").insert({"school_id": school["id"], "name": f"{name} Community"}).execute()
        print("created ", name)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "schools.example.txt")
