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
  model = Column(String) # drive model
  kern_name = Column(String, nullable=False) # sda, md0, etc. NOT unique over all hosts
  is_spinning_rust = Column(Boolean, nullable=False)
  label = Column(String) # disk label
  size_bytes = Column(BigInteger, nullable=False)
  fs_type = Column(String)
  type_ = Column(String, nullable=False) # disk, partition, raid0, etc
  
  last_seen = Column(DateTime, nullable=False)
  first_seen = Column(DateTime, nullable=False)

  # TODO: Add multi-column unique constraint for host + kern_name

  host = Column(String, ForeignKey("Hosts.name"), nullable=False)
  parent_id = Column(Integer, ForeignKey("BlockDevs.id")) # the disk that a partition belongs to, or the partitions composing a raid

