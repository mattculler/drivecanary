import sqlalchemy
import json
from datetime import datetime

from data_classes import Host, BlockDev, sqla_to_dict


class PyvDb(object):
  """DB wrapper for Pyvmind."""
  
  def __init__(self):
    self._engine = sqlalchemy.create_engine("sqlite:///pyvmind.db")
    Session = sqlalchemy.orm.sessionmaker(bind=self._engine)
    self._session = Session()

  def add_host(self, hostname):
    """We just saw a host - make sure it exists in the DB and update its last_seen time."""
    if len(self._session.query(Host).filter_by(name=hostname).all()) == 0:
      # A new host!  We must add it
      print("Adding host " + hostname)
      self._session.add(Host(
        name=hostname, 
        last_seen=datetime.now(), 
        first_seen=datetime.now()))
    else:
      # Just update the last seen
      print("Updating host " + hostname)
      self._session.query(Host).filter_by(name=hostname).first().last_seen = datetime.now()
    self._session.commit()


  def add_blockdevs(self, hostname, blockdev_json):
    """Import blockdevs to the DB from the JSON output from lsblk."""
#    print(json.dumps(blockdev_json, indent=2))
    for blkdev in blockdev_json["blockdevices"]:
      if blkdev["serial"] is None:
        # Skip non-disks for now
        continue
      if len(self._session.query(BlockDev).filter_by(serial=blkdev["serial"]).all()) == 0:
        # A new disk!  We must add it
        print("Adding disk " + blkdev["serial"])
        self._session.add(BlockDev(
          serial=blkdev["serial"],
          model=blkdev["model"], 
          kern_name=blkdev["kname"],
          is_spinning_rust=int(blkdev["rota"]),
          label=blkdev["label"],
          size_bytes=blkdev["size"],
          fs_type=blkdev["fstype"],
          type_=blkdev["type"],
          host=hostname,
          last_seen=datetime.now(), 
          first_seen=datetime.now()))
      else:
        # Update anything that has changed
        print("Updating disk " + blkdev["serial"])
        dbdev = self._session.query(BlockDev).filter_by(serial=blkdev["serial"]).first()
        dbdev.last_seen = datetime.now()
        dbdev.kern_name = blkdev["kname"]
        dbdev.label = blkdev["label"]
        dbdev.host = hostname # could have moved the disk to a different machine :D
        
    # TODO: A second pass to set the parent_ids, now that the disks are in place

    self._session.commit()

    
  def get_blockdevs(self):
    blkdevs = []
    for blkdev in self._session.query(BlockDev).all():
      blkdevs.append(sqla_to_dict(blkdev))
    return blkdevs
    
