"""The band a reading has to fall in, and how many readings are averaged."""

LOWER_BOUND = 0.5
"""The smallest reading that is still kept."""

UPPER_BOUND = 9.5
"""The largest reading that is still kept."""

MAX_READINGS = 64
"""How many readings one average may be taken over.

A caller that hands over a whole day's log is asking for an average of
something that is no longer one run, and the answer would look reasonable.
"""
