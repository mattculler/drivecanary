#!/usr/bin/python3

import os
import sys
import sqlalchemy

from data_classes import *

db_file = "pyvmind.db"

if __name__ == "__main__":
  if sys.argv[1] == "delete":
    print("Removing " + db_file)
    os.remove(db_file)

  # Create all tables
  print("Creating " + db_file)
  engine = sqlalchemy.create_engine("sqlite:///" + db_file)
  Base.metadata.create_all(engine)
