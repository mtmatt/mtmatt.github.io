+++
title = '小說'
# The section cascade below also matches this page, which must stay the index.
layout = 'list'

# To add a serial, create fiction/<work>/_index.md with a title and an optional
# description, and no date. Then add one file per chapter, such as 01.md, each
# with a title and date. Chapters run in date order, so give each a different
# date or time; weight is ignored. A work has no subfolders.
# Add completed = true to the work once its last chapter is out.
# To offer other reading orders, add them to the work. They replace date order on
# the work page and in chapter links. List a chapter at most once in an order.
# Naming an unpublished chapter fails the build:
#   [[orders]]
#   name = '故事時間'
#   chapters = ['03', '01', '02']
# To keep a non-linear work's chapters out of search, the sitemap and the site feed,
# so they can't give away its ending, add this to the work. It removes the work's
# own feed too. Plain keys such as outputs and completed go above the first [[...]]
# table, or TOML files them under it. Leave tags off these chapters, because tag
# pages would still list them.
#   outputs = ['html']
#   [[cascade]]
#   [cascade.build]
#   list = 'local'
#   [cascade.target]
#   kind = 'page'
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
