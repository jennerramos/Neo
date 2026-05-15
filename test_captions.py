from youtube_transcript_api import YouTubeTranscriptApi
try:
    tl = YouTubeTranscriptApi().list('1G7uW4DRHN0')
    found = list(tl)
    if found:
        for t in found:
            print(f"  {t.language_code:10} generated={t.is_generated}  name={t.language}")
    else:
        print("No transcripts found (empty list)")
except Exception as e:
    print(f"{type(e).__name__}: {e}")
