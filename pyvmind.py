#!/usr/bin/python3

import json
from datetime import date, datetime
from flask import Flask, request, render_template

from db import PyvDb
from driveindex import DriveIndex

app = Flask(__name__)
pyvdb = PyvDb()
driveindex = DriveIndex(["storage1", "storage2", "pve"], skip_update=True)

@app.route("/getblkdevs")
def get_blkdevs():
  blkdevs = pyvdb.get_blockdevs()
  return json.dumps(blkdevs)


def json_serial(obj):
	"""JSON serializer for objects not serializable by default json code"""

	if isinstance(obj, (datetime, date)):
		return obj.isoformat()
	raise TypeError ("Type %s not serializable" % type(obj))


@app.route("/")
def index():
  blkdevs = pyvdb.get_blockdevs()
  smarts = driveindex.get_latest_smart_per_drive_json()
  return render_template(
      "index.html", 
      blkdevs=json.dumps(blkdevs, default=json_serial),
      smarts=json.dumps(smarts))

if __name__ == "__main__":
  app.run(debug=True, host="0.0.0.0")
