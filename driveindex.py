import os
import paramiko
import json
import collections
from datetime import datetime

import util
from data_classes import BlockDev
import smartruns


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
    yield from self._janky_ansible(
        "lsblk -Jbo KNAME,SIZE,FSTYPE,LABEL,MODEL,SERIAL,TYPE,ROTA,PKNAME")


  def get_drive_info(self, hostname, kern_name):
    """Returns smartctl drive info for the host and device (smartctl -i /dev/whatever).
    kern_name is just the node name, not a path.
    """
    client = self._host_pool[hostname]
    stdin, stdout, stderr = client.exec_command("smartctl -i /dev/{0}".format(kern_name))

    # Returns a defaultdict because all drives may not have the same set of keys, and 
    #  that's ok.
    info = collections.defaultdict(lambda: None)
    for info_line in stdout.read().decode("utf-8").splitlines():
      # Split string into two on the first colon - this fixes the case where there are other 
      #  colons in the value.
      key, value = util.split_on_first(info_line)
      if not key:
        # This omits blank lines and also the informational, non-key/value output, which 
        #  IMHO should have gone to stderr (smartmontools! <shakes fist>)
        continue

      # Special case - smart available and smart enabled would both wind up with the same 
      #  key,  so we have to set them to something else.
      if key == "SMART support is":
        if "ambiguous" in value.lower():
          # Let it be null
          continue
        elif "available" in value.lower():
          key = "smart_avail"
          value = ("Available" in value) # as opposed to "Unavailable"
        elif "abled" in value:
          key = "smart_enabled"
          value = ("Enabled" in value) # as opposed to "Disabled", presumably
      else:
        # Not a bool
        value = value.strip()

      info[key] = value
    return info
    

  def get_drive_details(self, hostname, kern_name):
    """Returns smartctl drive details for the host and device (smartctl -i /dev/whatever).
    kern_name is just the node name, not a path.
    """
    def _all_upper_or_space(key):
      for char in key:
        if not char.isupper() and not char.isspace():
          return False
      return True

    client = self._host_pool[hostname]
    stdin, stdout, stderr = client.exec_command(
        "smartctl -P show /dev/{0}".format(kern_name))

    # Returns a defaultdict because not all drives have the same set of keys, and that's ok
    details = collections.defaultdict(lambda: None)
    for detail_line in stdout.read().decode("utf-8").splitlines():
      # Split string into two on the first colon - this fixes the case where there are other 
      #  colons in the value.
      key, value = util.split_on_first(detail_line)
      if not key or not _all_upper_or_space(key):
        # This omits blank lines and also the informational, non-key/value output, which 
        #  IMHO should have gone to stderr (smartmontools! <shakes fist>)
        continue
      details[key] = value.strip()
    return details


  def update_smart_reports(self, hostname, dev):
    """Writes updated SMART reports out to disk, if enough time has elapsed since they were
    last gathered.
    """
    # Get the data and write it out
    # TODO: Check to see when the latest report was gotten, and make sure enough time has 
    #  elapsed.
    client = self._host_pool[hostname]
    stdin, stdout, stderr = client.exec_command("smartctl -a /dev/{0}".format(dev.kern_name))
    smartruns.write_run(dev.serial, stdout.read().decode("utf-8"))
    

class DriveIndex(object):
  """Owns the Puppetmaster, making several requests to it and aggregating drive data, making
  it ready to pass to the DB.
  """

  def __init__(self, hosts, skip_update=False):
    self._puppets = Puppetmaster(hosts)
    self._blockdevs = []

    # Create the sqlalchemy objects from the Puppetmaster results
    for hostname, json_str in self._puppets.get_lsblk_iterable():
      try:
        blockdev_json = json.loads(json_str)
      except json.decoder.JSONDecodeError as e:
        # Mayhap this system is really old and doesn't support the lsblk -J 
        #  flag?
        continue
      for blkdev in blockdev_json["blockdevices"]:
        if blkdev["serial"] is None:
          # Skip non-disks for now (we'll be skipping mdadm arrays, mostly)
          # TODO: Add these mdadm arrays to the dataset as well
          continue 

        # Build metadata drive object
        info = self._puppets.get_drive_info(hostname, blkdev["kname"])
        details = self._puppets.get_drive_details(hostname, blkdev["kname"])
        dev = BlockDev(
          serial=blkdev["serial"], 
          model=blkdev["model"],  
          is_spinning_rust=int(blkdev["rota"]), 
          size_bytes=blkdev["size"], 
          type_=blkdev["type"], 
          model_family=details["MODEL FAMILY"],
          firmware=details["FIRMWARE"],
          rpm=info["Rotation Rate"],
          ata_ver=info["ATA Version is"],
          sata_ver=info["SATA Version is"],
          smart_avail=info["smart_avail"],
          fs_type=blkdev["fstype"], 
          label=blkdev["label"], 
          kern_name=blkdev["kname"], 
          smart_enabled=info["smart_enabled"],
          last_seen=datetime.now(),
          host=hostname)
        self._blockdevs.append(dev)

        # Update stored SMART reports
        if not skip_update:
          self._puppets.update_smart_reports(hostname, dev)

  def get_blockdevs(self):
    """Yields each Blockdev."""
    for dev in self._blockdevs:
      yield dev

  def get_latest_smart_per_drive(self):
    """Yields SmartRun objects of the most recent SMART run for each drive.
    """
    for dev in self._blockdevs:
      yield dev, smartruns.get_latest_run(dev.serial)

  def get_latest_smart_per_drive_json(self):
    """Returns a list of SmartRun JSON objects of the most recent SMART run 
    for each drive.
    """
    all_reports = {}
    for dev, smartrun in self.get_latest_smart_per_drive():
      all_reports[dev.serial] = smartrun.to_json()
    return all_reports

  def get_all_smarts_for_drive_json(self, serial):
    all_reports = []
    for smartrun in smartruns.get_all_runs_for_drive(serial):
      all_reports.append(smartrun.to_json())
    return all_reports


