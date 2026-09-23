"""Run: python3 test_text_filter.py"""
from text_filter import clean, orphan_signs

lines = ["வணக்கம். வணக்கம். வணக்கம்.", "hello world", "அவன் வந்தான்.", "ாதவறு"]
cleaned, dropped, repeats, notes = clean(lines, [.9, .9, .65, .3])
assert dropped == 1 and repeats == 2
assert cleaned == ["வணக்கம்.", "அவன் வந்தான்.", "ாதவறு"]
assert [note[1] for note in notes] == ["KEEP", "FLAG", "REVIEW"]
assert orphan_signs("கா") == [] and orphan_signs("ாத") == [1]
print("text_filter checks passed")
