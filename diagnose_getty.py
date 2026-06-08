"""Quick standalone check of the Getty reconcile endpoint from YOUR machine.

Run:  python diagnose_getty.py
Use this only if `lookup` still returns 0 candidates after updating aat.py.
It prints the raw response so we can see the id format and overall shape.
"""
import json

import requests

URL = "https://services.getty.edu/vocab/reconcile/"
Q = {"q0": {"query": "lithography", "type": "/aat", "limit": 5}}


def show(label, **kwargs):
    print(f"\n== {label} ==")
    try:
        r = requests.post(URL, timeout=30, **kwargs)
        print("status:", r.status_code, "| content-type:", r.headers.get("content-type"))
        print(r.text[:900])
    except Exception as exc:  # noqa: BLE001
        print("error:", repr(exc))


if __name__ == "__main__":
    show("JSON body", json={"queries": Q})
    show("form-encoded", data={"queries": json.dumps(Q)})
    print(
        "\nWhat to look for: a block like {\"q0\": {\"result\": [{\"id\": \"aat/300053271\", "
        "\"name\": \"lithography\", ...}]}}. If you instead see a 'versions'/'name' object, "
        "that's the service manifest (the request wasn't recognised as a query)."
    )
