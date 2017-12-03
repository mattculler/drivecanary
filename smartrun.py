import util

class SmartRun(object):
  """Data structure that parses and represents the output of smartctl -a /dev/whatever"""

  def __init__(self, text_file=None, text=None):
    """Supply either the text_file containing the output of smartctl -a, or text, containing
    the same.
    """
    # Everything gets parsed out into these members
    self._info = {}
    self._healthy = None
    self._general_smart = []
    self._attributes_rev = None
    self._vendor_smart = {}
    self._errors = {
      "version": None,
      "error_count": None,
      "log": ""
    }
    self._test_log = ""
    self._selective_test_log = ""

    if text_file:
      # Don't need to go get it
      self._parse_smart_text_file(text_file)
    elif text:
      self._parse_smart(text)
    else:
      raise Exception("SMART output not specified!")

  def _parse_smart_text_file(self, text_file):
    with open(text_file, "r") as f:
      self._parse_smart(f.read())

  def _parse_smart(self, text):
    section = None
    for line in text.splitlines():
      if not line.strip():
        continue

      # We have no use for tabs, they just make it more complicated
      line = line.replace("\t", " ")

      # The smart log announces a section with an entire line, so if we find that line, 
      #  then set a variable announcing what section we are entering and continue into it.
      if line.startswith("=== START OF INFORMATION SECTION ==="):
        section = "info"
        continue
      elif line.startswith("=== START OF READ SMART DATA SECTION ==="):
        section = "overallhealth"
        continue
      elif line.startswith("General SMART Values:"):
        section = "general"
        continue
      elif line.startswith("SMART Attributes Data Structure revision number:"):
        section = "attributes"
        # one-line section - do not continue
      elif line.startswith("Vendor Specific SMART Attributes with Thresholds:"):
        section = "vendor"
        continue
      elif line.startswith("SMART Error Log Version:"):
        section = "error"
        # first line matters - do not continue
      elif line.startswith("SMART Self-test log structure revision number"):
        section = "selftest"
        # first line matters - do not continue
      elif line.startswith("SMART Selective self-test log data structure revision number"):
        section = "selective"
        # first line matters - do not continue
      
      if not section:
        # Skip the junk at the beginning
        pass
      elif section == "info":
        key, value = util.split_on_first(line)
        self._info[key] = value.strip()
        continue
      elif section == "overallhealth":
        _, assessment = line.split(":")
        self._healthy = assessment.strip() == "PASSED"
        continue
      elif section == "general":
        # This is complicated because these values are split over multiple lines.
        # Each of these entries has a key, value, and explanation.  Keys may be split 
        #  over multiple lines, but their last line will always be the same line as the
        #  value, which will also always be the same line that the explanation starts on.
        #  The explanation may continue over multiple lines as well.  And they are all 
        #  justified, so the value is in the center.  Fun stuff.
        if not line.startswith(" ") and "(" not in line:
          # Start of a key line that continues
          self._general_smart.append({"key": line.strip()})
        elif not line.startswith(" ") and "(" in line:
          # Value line - but is it the beginning of the value?  Check the last one
          left_bound = line.find("(")
          right_bound = line.find(")")
          key = line[:left_bound].strip(": ")
          value = line[left_bound+1:right_bound].strip()
          explanation = line[right_bound+1:].strip()

          if not self._general_smart or "value" in self._general_smart[-1]:
            # New value line
            self._general_smart.append({
              "key": key,
              "value": value,
              "explanation": explanation
            })
          else:
            # Continuation of the previous key
            self._general_smart[-1]["key"] += " " + key
            self._general_smart[-1]["value"] = value
            self._general_smart[-1]["explanation"] = explanation
        elif line.startswith(" "):
          # Continuation of an explanation
          self._general_smart[-1]["explanation"] += " " + line.strip()
        else:
          raise Exception("Output is crazy!")
        continue
      elif section == "attributes":
        _, rev = line.split(":")
        self._attributes_rev = rev.strip()
        continue
      elif section == "vendor":
        if line.startswith("ID#"):
          # The column headings
          continue
        
        values = line.split(maxsplit=9)
        attr_id = values[0]
        self._vendor_smart[attr_id] = {
          "id": attr_id,
          "name": values[1],
          "flag": values[2],
          "value": values[3],
          "worst": values[4],
          "thresh": values[5],
          "type": values[6],
          #"updated": values[7], # who cares, allegedly inaccurate
          "when": values[8],
          "raw": values[9]
        }
        continue
      elif section == "error":
        if line.startswith("SMART Error Log Version:"):
          _, ver = util.split_on_first(line)
          self._errors["version"] = ver
        elif line.startswith("No Errors Logged"):
          self._errors["error_count"] = 0
        elif line.startswith("ATA Error Count:"):
          _, count = util.split_on_first(line)
          self._errors["error_count"] = count
        else:
          # TODO - Parse the log further?
          self._errors["log"] += line + "\n"
        continue
      elif section == "selftest":
        # TODO
        self._test_log += line + "\n"
        continue
      elif section == "selective":
        # TODO
        self._selective_test_log += line + "\n"
        continue
      print(line)

  def to_json(self):
    return {
        "info": self._info,
        "healthy": self._healthy,
        "general_smart": self._general_smart,
        "attributes_rev": self._attributes_rev,
        "vendor_smart": self._vendor_smart,
        "errors": self._errors,
        "test_log": self._test_log,
        "selective_test_log": self._selective_test_log
    }


# DELETEME - testing only
import os
import json
for d in os.listdir("testdata"):
  print()
  print("*** OPERATING ON {0} ***".format(d))
  print()
  s = SmartRun(os.path.join("testdata", d))

  print(json.dumps(s.to_json(), indent=2))
