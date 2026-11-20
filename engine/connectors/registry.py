from .kick import Kick
from .twitch import Twitch
from .youtube import YouTube

# Order = display order in the dashboard. New platform: import it and add here.
CONNECTORS = [Twitch, Kick, YouTube]
