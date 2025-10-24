---
title: "OwoJudge"
date: 2025-10-24
draft: false
description: "The next-generation DSA judge system"
tags: ["projects", "programming", "judge"]
---

## Motivation

After being a TA in DSA course and preparing the programming asignments, I realized the inconvenience of the existing DSA judge system. The existing judge system has several limitations, including bare metal installation, out of date dependencies, and inconvenient task preparation.

Besides, professors and our team want to add some new features to the system. However, after our team exploring the code base of the existing system, we found that it may be easier to build a new one from scratch.


## Features

- Our new judge system is containerized using Docker, which makes it easy to deploy and scale.
- It is built using modern no-SQL database: MongoDB, which ensures that the system is more secured.
- It supports a slightly modified TPS (Task Preparation System), which enhance the task preparation workflow for TAs.


## My role in this project

I am the backend developer of the OwoJudge project. My responsibilities include providing backend APIs using Node.js and Express, setup isolate sandbox, and using the Dockerfile and docker-compose for deployment.

Additionally, I decided to provide a CLI tool to use the judge system. The CLI tool is prepared for those use neovim, or people who want to have a light weight experience.
