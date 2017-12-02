import sqlalchemy
from sqlalchemy import Column, ForeignKey, Integer, String, Boolean, BigInteger, DateTime
from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()


class Host(Base):
  __tablename__ = "Hosts"

  name = Column(String, primary_key=True, nullable=False, unique=True)
  last_seen = Column(DateTime, nullable=False)
  first_seen = Column(DateTime, nullable=False)


class BlockDev(Base):
  __tablename__ = "BlockDevs"

  id = Column(Integer, primary_key=True)
  serial = Column(String, unique=True) # unique serial of drive
  model = Column(String) # drive model, from the kernel
  is_spinning_rust = Column(Boolean, nullable=False) # is HDD?  (if false, is SSD)
  size_bytes = Column(BigInteger, nullable=False)
  type_ = Column(String, nullable=False) # disk, partition, raid0, etc
  model_family = Column(String) # for most drives, smartctl can give a user-friendly name
  firmware = Column(String) # FW version
  rpm = Column(String) # nullable, to account for SSD
  ata_ver = Column(String)
  sata_ver = Column(String)
  smart_avail = Column(Boolean, nullable=False)

  # TODO: Changeable, and a history would be useful - move to own table?
  fs_type = Column(String)
  label = Column(String) # disk label
  kern_name = Column(String, nullable=False) # sda, md0, etc. NOT unique over all hosts
  smart_enabled = Column(Boolean, nullable=False)
  
  last_seen = Column(DateTime, nullable=False)
  first_seen = Column(DateTime, nullable=False)

  # TODO: Add multi-column unique constraint for host + kern_name

  host = Column(String, ForeignKey("Hosts.name"), nullable=False)
  parent_id = Column(Integer, ForeignKey("BlockDevs.id")) # the disk that a partition belongs to, or the partitions composing a raid


def sqla_to_dict(sqltype):
  """Returns JSON from a sqlalchemy type (inherits from declarative_base)."""
  return {c.name: getattr(sqltype, c.name) for c in sqltype.__table__.columns}
