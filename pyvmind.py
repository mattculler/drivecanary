#!/usr/bin/python3

import json
from flask import Flask, request, render_template

from db import PyvDb

app = Flask(__name__)
pyvdb = PyvDb()

@app.route("/getblkdevs")
def get_blkdevs():
  blkdevs = pyvdb.get_blockdevs()
  return json.dumps(blkdevs)


from datetime import date, datetime
def json_serial(obj):
	"""JSON serializer for objects not serializable by default json code"""

	if isinstance(obj, (datetime, date)):
		return obj.isoformat()
	raise TypeError ("Type %s not serializable" % type(obj))


@app.route("/")
def index():
  blkdevs = pyvdb.get_blockdevs()
  return render_template("index.html", blkdevs=json.dumps(blkdevs, default=json_serial))

if __name__ == "__main__":
  app.run(debug=True, host="0.0.0.0")
