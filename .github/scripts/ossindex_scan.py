#!/usr/bin/env python3
import sys, os, json, base64, time
from urllib import request, error

API_URL = "https://ossindex.sonatype.org/api/v3/component-report"
BATCH_SIZE = 100  # API allows up to 128

def read_coords(path, ignore_path=None):
    coords = []
    with open(path) as f:
        coords = [l.strip() for l in f if l.strip()]
    ignore = set()
    if ignore_path and os.path.exists(ignore_path):
        with open(ignore_path) as ig:
            for l in ig:
                l = l.strip()
                if not l or l.startswith("#"): continue
                if l.startswith("pkg:"): ignore.add(l)
    return [c for c in coords if c not in ignore]

def post_batch(coords, auth=None):
    payload = json.dumps({"coordinates": coords}).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "ossindex-gha/1.0",
    }
    if auth:
        headers["Authorization"] = f"Basic {auth}"
    req = request.Request(API_URL, data=payload, headers=headers, method="POST")
    with request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))

def main():
    if len(sys.argv) < 2:
        print("Usage: ossindex_scan.py coords.txt", file=sys.stderr)
        sys.exit(2)
    coords_path = sys.argv[1]
    username = os.getenv("OSSINDEX_USERNAME", "")
    token = os.getenv("OSSINDEX_TOKEN", "")
    auth = None
    if username and token:
        auth = base64.b64encode(f"{username}:{token}".encode("utf-8")).decode("ascii")

    coords = read_coords(coords_path, os.getenv("AUDITIGNORE_PATH"))
    if not coords:
        print("No coordinates to scan.")
        return

    total = len(coords)
    print(f"Scanning {total} coordinates with Sonatype OSS Index...")
    all_findings = []
    for i in range(0, total, BATCH_SIZE):
        batch = coords[i:i+BATCH_SIZE]
        try:
            data = post_batch(batch, auth)
        except error.HTTPError as e:
            msg = e.read().decode("utf-8", errors="ignore")
            print(f"HTTP {e.code}: {msg}", file=sys.stderr)
            sys.exit(1)
        except Exception as e:
            print(f"Request error: {e}", file=sys.stderr)
            sys.exit(1)

        for comp in data or []:
            vulns = comp.get("vulnerabilities", []) or []
            if vulns:
                all_findings.append({
                    "coordinate": comp.get("coordinates"),
                    "vulnerabilities": [{
                        "id": v.get("id"),
                        "title": v.get("title") or v.get("displayName"),
                        "cvssScore": v.get("cvssScore"),
                        "reference": v.get("reference"),
                    } for v in vulns]
                })
        # be polite to API
        time.sleep(0.2)

    if not all_findings:
        print("No vulnerabilities found.")
        return

    print("\nVulnerabilities found:")
    for comp in all_findings:
        print(f"- {comp['coordinate']}")
        for v in comp["vulnerabilities"]:
            score = v["cvssScore"]
            print(f"  • {v['id']} (CVSS: {score}) — {v['title']}\n    {v['reference']}")

    print(f"\nTotal affected components: {len(all_findings)}")
    # Fail the job on any finding; adjust policy as needed
    sys.exit(1)

if __name__ == "__main__":
    main()