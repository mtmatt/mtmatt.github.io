+++
title = '小說'
# The section cascade below also matches this page, which must stay the index.
layout = 'list'

# To add a serial, create fiction/<work>/_index.md with a title and an optional
# description, and no date. Then add one file per chapter, such as 01.md, each
# with a title and date. Chapters run in date order, so give each a different
# date or time; weight is ignored. A work has no subfolders.
# Add completed = true to the work once its last chapter is out.
# Stories short enough for one page stay in blogs with layout = 'reading'.
[[cascade]]
layout = 'work'
[cascade.target]
kind = 'section'

[[cascade]]
layout = 'reading'
[cascade.target]
kind = 'page'
+++
