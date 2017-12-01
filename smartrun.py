import util

class SmartRun(object):
  """Data structure that parses and represents the output of smartctl -a /dev/whatever"""

  def __init__(self, text_file=None):
    # Everything gets parsed out into these members
    self._info = {}
    self._overall_health = None
    self._general_smart = []
    self._attributes_rev = None

    if text_file:
      # Don't need to go get it
      self._parse_smart_text_file(text_file)
    else:
      raise Exception("SMART output not specified!")

  def _parse_smart_text_file(self, text_file):
    with open(text_file, "r") as f:
      section = None
      for line in f.readlines():
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
        
        if not section:
          # Skip the junk at the beginning
          pass
        elif section == "info":
          key, value = util.split_on_first(":", line)
          self._info[key] = value
          continue
        elif section == "overallhealth":
          _, assessment = line.split(":")
          self._overall_health = assessment.strip() == "PASSED"
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
          _, self._attributes_rev = line.split(":")
          continue
        elif section == "vendor":
          # TODO: The meat 'n' pertaters
          pass
        elif section == "error":
          # TODO: Not the meat 'n' pertaters, but very important
          pass
        print(line, end="")
    import json
    print(json.dumps(self._general_smart, indent=2), end="")


# DELETEME - testing only
SmartRun("testdata/storage2.smart")
