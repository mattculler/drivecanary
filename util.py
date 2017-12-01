def split_on_first(delim, string):
  """Splits the string on the first occurrence of the delimiter, and returns a 2-tuple.
  If the delimiter does not exist in the string, returns (None, None).
  """
  try:
    left, right = string.split(delim, maxsplit=1)
  except ValueError:
    return None, None
  return left, right
#  i = string.find(delim)
#  if i == -1:
#    return None, None
#  key = string[:i]
#  value = string[i + 1:].strip()
#  return key, value
