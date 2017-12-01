#!/usr/bin/python3
# Queries the linked servers and updates the DB with new SMART info.

import os
import paramiko
import json
import collections
from datetime import datetime

from data_classes import Host, BlockDev, sqla_to_dict
from db import PyvDb


class Puppetmaster(object):
  """Controls all client connections."""

  def __init__(self, hosts):
    """hosts - A list of hosts to open connections to."""
    self._config = paramiko.config.SSHConfig()
    with open("/etc/ssh/ssh_config", "r") as f:
      self._config.parse(f)

    self._host_pool = {}
    for hostname in hosts:
      self._init_host_connection(hostname)

  def _init_host_connection(self, hostname):
    """Initiate a connection to a remote hostname and add it to the pool."""
    client = paramiko.SSHClient()
    client.load_host_keys(os.path.expanduser("~/.ssh/known_hosts"))
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    hostconfig = self._config.lookup(hostname)
    client.connect(hostname, username=hostconfig["user"])
    self._host_pool[hostname] = client

  def _janky_ansible(self, cmd):
    """lol"""
    for hostname, client in self._host_pool.items():
      stdin, stdout, stderr = client.exec_command(cmd)
      yield hostname, stdout.read().decode("utf-8")

  def __del__(self):
    for hostname, client in self._host_pool.items():
      client.close()


  def get_lsblk_iterable(self):
    """Yields a pair of (hostname, lsblk JSON) for each host."""
    yield from self._janky_ansible("lsblk -Jbo KNAME,SIZE,FSTYPE,LABEL,MODEL,SERIAL,TYPE,ROTA,PKNAME")

  def get_drive_details(self, hostname, kern_name):
    """Returns smartctl drive details for the host and device.
    kern_name is just the node name, not a path.
    """
    def _all_upper_or_space(key):
      for char in key:
        if not char.isupper() and not char.isspace():
          return False
      return True

    client = self._host_pool[hostname]
    stdin, stdout, stderr = client.exec_command("smartctl -P show /dev/{0}".format(kern_name))

    # Returns a defaultdict because not all drives have the same set of keys, and that's ok
    details = collections.defaultdict(lambda: None)
    for detail_line in stdout.read().decode("utf-8").splitlines():
      # Split string into two on the first colon - this fixes the case where there are other 
      #  colons in the value.
      first_colon_i = detail_line.find(":")
      key = detail_line[:first_colon_i]
      if not key or not _all_upper_or_space(key):
        # This omits blank lines and also the informational, non-key/value output, which IMHO 
        #  should have gone to stderr (smartmontools! <shakes fist>)
        continue
      value = detail_line[first_colon_i + 1:].strip()
      details[key] = value
    return details
      

class DriveIndex(object):
  """Owns the Puppetmaster, making several requests to it and aggregating drive data, making it 
  ready to pass to the DB.
  """

  def __init__(self, hosts):
    self._puppets = Puppetmaster(hosts)
    self._blockdevs = []

    # Create the sqlalchemy objects from the Puppetmaster results
    for hostname, json_str in self._puppets.get_lsblk_iterable():
      blockdev_json = json.loads(json_str)
      for blkdev in blockdev_json["blockdevices"]:
        if blkdev["serial"] is None:
          # Skip non-disks for now (we'll be skipping mdadm arrays, mostly)
          # TODO: Add these mdadm arrays to the dataset as well
          continue 

        details = self._puppets.get_drive_details(hostname, blkdev["kname"])

        print(hostname, blkdev["kname"])
        print(json.dumps(details, indent=2))

        dev = BlockDev( 
          serial=blkdev["serial"], 
          model=blkdev["model"],  
          is_spinning_rust=int(blkdev["rota"]), 
          size_bytes=blkdev["size"], 
          type_=blkdev["type"], 
          model_family=details["MODEL FAMILY"],
          firmware=details["FIRMWARE"],
          fs_type=blkdev["fstype"], 
          label=blkdev["label"], 
          kern_name=blkdev["kname"], 
          last_seen=datetime.now(),
          host=hostname)
        self._blockdevs.append(dev)

  def get_blockdevs(self):
    """Yields each Blockdev."""
    for dev in self._blockdevs:
      yield dev


if __name__ == "__main__":
  pyvdb = PyvDb()
 
  # Build data structures and update DB
  hosts = ["storage1", "storage2"]
  for host in hosts:
    pyvdb.add_host(host)
  drives = DriveIndex(hosts)

  for dev in drives.get_blockdevs():
    pyvdb.add_blockdev(dev)

  del drives 
