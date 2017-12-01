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

  def add_blockdev(self, dev):
    """Adds or updates a blockdev."""
    if not dev.serial:
      # Skip non-disks for now
      return
    if len(self._session.query(BlockDev).filter_by(serial=dev.serial).all()) == 0:
      # A new disk!  We must add it
      print("Adding disk " + dev.serial)
      dev.first_seen = datetime.now()
      self._session.add(dev)
    else:
      # Update anything that has changed
      print("Updating disk " + dev.serial)

      # Get the version from the DB so first_seen is correct
      dbdev = self._session.query(BlockDev).filter_by(serial=dev.serial).first()
      dbdev.last_seen = datetime.now()
      dbdev.fs_type = dev.fs_type
      dbdev.kern_name = dev.kern_name
      dbdev.label = dev.label
      dbdev.host = dev.host # could have moved the disk to a different machine :D
      
    # TODO: A second pass to set the parent_ids, now that the disks are in place

    self._session.commit()
    

  def get_blockdevs(self):
    blkdevs = []
    for blkdev in self._session.query(BlockDev).all():
      blkdevs.append(sqla_to_dict(blkdev))
    return blkdevs
    
