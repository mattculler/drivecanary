#!/usr/bin/python3
# Queries the linked servers and updates the DB with new SMART info.

import os
import paramiko
import json

from pyvdb import PyvDb


class Puppetmaster(object):
  """Controls all client connections."""

  def __init__(self, db, hosts):
    """hosts - A list of hosts to open connections to."""
    self._db = db

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

    db.add_host(hostname)

  def _janky_ansible(self, cmd):
    """lol"""
    for hostname, client in self._host_pool.items():
      stdin, stdout, stderr = client.exec_command(cmd)
      yield hostname, stdout.read().decode("utf-8")
      #o = stdout.read().decode("utf-8")
#      with open(hostname + ".smart", "w") as f:
#        f.write(o)
      #import ptpdb; ptpdb.set_trace()


  def __del__(self):
    for hostname, client in self._host_pool.items():
      client.close()


if __name__ == "__main__":
  db = PyvDb()
  p = Puppetmaster(db, ["storage1", "storage2"])
  
  for hostname, json_str in p._janky_ansible("lsblk -Jbo KNAME,SIZE,FSTYPE,LABEL,MODEL,SERIAL,TYPE,ROTA,PKNAME"):
    disk_json = json.loads(json_str)
    #print(json.dumps(disk_json, indent=2))
    db.add_blockdevs(hostname, disk_json)
  
  del p
