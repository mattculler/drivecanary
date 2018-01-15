#!/usr/bin/python3
# Queries the linked servers and updates the DB with new SMART info.

from db import PyvDb


if __name__ == "__main__":
  pyvdb = PyvDb()
 
  # Build data structures and update DB
  hosts = ["storage1", "storage2", "pve"]
  for host in hosts:
    pyvdb.add_host(host)
  drives = DriveIndex(hosts)

  for dev in drives.get_blockdevs():
    # Make sure it's in the DB
    pyvdb.add_blockdev(dev)

  del drives 
