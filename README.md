# Trak-and-BeatGrab
Traktrain and BeatStars stream grabbers (interactive CLI) 


==================================================



Downloads BeatStars stream audio for an artist profile or a single beat.

The on-site player loads audio as HLS (.m3u8 + split .ts segments under
content.beatstars.com). This script instead uses BeatStars' stream API:

  https://main.v2.beatstars.com/stream?id=<TRACK_ID>&return=audio

which returns the full MP3/WAV in one file (no .ts stitching needed).



==================================================


Downloads the streamable MP3s from a Traktrain producer profile page
(the same files the on-site player uses). Files are saved under:

  _output/<artist-slug>/


===================================================
