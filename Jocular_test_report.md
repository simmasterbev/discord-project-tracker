# Discord Project Tracker — Initial Test Report

Hey, I ran the first basic test on the old Discord server.

## What I tested

The bot successfully:

- Created a project called **Test Project**
- Added three tasks to it
- Assigned the tasks to me
- Tracked weighted progress
- Updated progress from `0% → 25% → 75% → 100%`
- Announced when the project was complete
- Created a **Test Roadmap** tech tree
- Added a milestone called **Project plan complete**
- Linked the completed project to that milestone
- Automatically marked the milestone complete
- Awarded XP to the contributor
- Generated a readable image of the tech tree

The basic project tracking and tech-tree flow are working.

## Current observations

The tech-tree image currently shows:

- The tree name
- How many milestones are complete
- Milestone name
- Completion status
- XP
- Progress bar
- What the milestone unlocks

The image does not currently show:

- Who is working on the milestone
- A separate milestone description
- An active/in-progress milestone yet—we only tested a completed one so far

Those are the next things I would test or potentially add, since they were part of the original goal.

## Simple testing guide

### 1. Create a project

Use:

```text
/project new
```

Example values:

```text
name: Test Project
description: Testing the project tracker
```

### 2. Add tasks

Use:

```text
/task add
```

Example values:

```text
project: Test Project
title: Create the project outline
assignee: yourself
weight: 1
```

Add several tasks with different weights if you want to test progress calculations.

### 3. Update tasks

To complete a task, use:

```text
/task done
```

Then enter the task number.

You can also use:

```text
/task status
```

to change a task to `todo`, `doing`, `blocked`, or `done`.

### 4. Create a tech tree

Use:

```text
/tree new
```

Example values:

```text
key: test
name: Test Roadmap
description: Testing the milestone flow
```

### 5. Add a milestone

Use:

```text
/tree add
```

Example values:

```text
key: plan
name: Project plan complete
unlocks: Begin implementation
xp: 100
tree: test
```

### 6. Link the project

Use:

```text
/tree link
```

Example values:

```text
key: plan
project: Test Project
```

The milestone should update based on the project’s progress.

### 7. View the tree

Use:

```text
/tree show
```

Select the tree named `test`.

The bot should send an image showing the current state of the tree.

## Overall result

The first test was successful. The main next step is testing an unfinished project and a milestone with prerequisites so we can verify the active, locked, and unlocked states.
