SmartCourse — Intelligent Course Delivery Platform [Part
- A]
EduCorp is building SmartCourse, an intelligent, large-scale learning platform designed to
support modern digital education for universities, enterprises, and training academies. The
company is experiencing rapid growth in enrolled learners and instructors, which has exposed
several limitations in their current systems:
● Content publishing is slow and manual, making it difficult for instructors to launch new
courses and update existing ones.
● Students struggle to find relevant information, as the platform lacks intelligent search,
contextual assistance, and adaptive learning support.
● Course data, user progress, and analytics are scattered, causing inconsistencies
between reporting dashboards and the actual state of the platform.
● High user traffic results in delays when processing large volumes of enrollments,
notifications, and background tasks.
● Course interactions generate rich but underutilized data, which is not being leveraged for
recommendations or learning enhancement.
To address these challenges, EduCorp is commissioning a new backend for SmartCourse with
the following business goals:
Description / Problem Statement
This part focuses on building the foundational backend system of SmartCourse, addressing
scalability, consistency, and reliability challenges in course management, publishing,
enrollments, and analytics.
The goal is to solve:
● Slow and manual content publishing workflows
● Data inconsistency across course, enrollment, and analytics systems
● High latency under heavy traffic
● Lack of reliable background processing
● Weak observability and failure recovery
This layer ensures a robust, scalable, and event-driven platform without introducing GenAI
capabilities.

Business Goals & Vision
SmartCourse must provide:
A robust course management system
● Instructors create courses, define modules, upload learning materials, and publish
updates.
● Students browse courses, enroll, track their learning progress, and interact with content.
A scalable and reliable operations backbone
● Publishing a course triggers multiple internal processes such as indexing, content
extraction, and preparation for intelligent search.
● Enrollment triggers progress initialization, analytics updates, and notifications.
Consistent and accurate learner data
● The state of enrollments, progress, completions, and certificates must be reliable,
durable, and easy to query.
A foundation that supports long-term scalability
● As the platform grows, SmartCourse must handle tens of thousands of learners
concurrently, along with spikes during course launches or corporate training schedules.
● Background workflows should run reliably even under heavy load.
Core Functional Requirements
1. Course & User Management
● Creation and updating of courses, modules, and learning assets
● User registration with appropriate roles (student/instructor/admin)
● Student enrollment into courses, including rules for:
○ Duplicate enrollments
○ Enrollment limits or prerequisites
○ Enrollment history

Each update or enrollment must ensure consistency across all parts of the system.

2. Content Publishing Workflow
When an instructor publishes or updates a course:
● The content must be analyzed and broken into components (modules, lessons, chunks).
● Relevant data should be stored in a structure that supports:
○ Fast retrieval
○ Search
● The platform must mark the course as “ready” once all internal processing completes.
● Partial failures must not corrupt the publishing workflow.
3. Enrollment Workflow
When a student enrolls in a course:
● Their enrollment is recorded.
● Their progress tracking is initialized.
● Analytics records must be updated to reflect platform activity.
● Notifications may need to be triggered (e.g., “Welcome to the course”).
This workflow must handle:
● High volume
● Idempotency
● Backpressure handling
● Recovery from failures without losing user state
4. Distributed & Event-Driven Behaviors
● Content processing after publishing
● Updating analytics after enrollment
● Sending notifications
● Preparing course material for intelligent Q&A
These tasks should:
● Run independently from the main user flows
● Be traceable and recoverable
● Handle failures gracefully
● Avoid double-processing
● Support workload spikes

●
5. Analytics Metrics
● Total Students
● Total Instructors
● Total Courses Published
● New Enrollments Over Time
● Course Completion Rate
● Average Time to Complete a Course
● Most Popular Courses
● Average Courses per Student
● Failed Events / Workflow Issues
●
6. System Observability & Reliability Expectations
● Clear separation of responsibilities between components
● Monitoring and logging for all key flows
● Ability to diagnose failures in:
○ Course publishing
○ Enrollment progression
○ Background tasks
● High consistency and accuracy across all data models
Expected Outcomes
● Support all major course lifecycle operations
● Reliable background processing (publishing, analytics, notifications)
● High scalability under load
● Strong consistency and failure handling
● High-quality architecture and maintainability
● PRD Requirement
○ Key use-cases
○ Functional and non-functional requirements
○ Timeline / milestones
○ Traceability between features and deliverables

●
Tech Stack
Backend:
● Python, FastAPI
● PostgreSQL
● NoSQL DB
● Redis
● Celery Workers with RabbitMQ
● Kafka + Schema Registry
● Temporal (workflows)
Observability:
● Prometheus + Grafana
● Jaeger
● OpenTelemetry
DevOps:
● Docker
● Docker Compose