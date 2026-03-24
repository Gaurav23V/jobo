# Introduction

Hey, I'm Gaurav Verma — a Computer Science engineer based out of Jaipur, India, wrapping up my degree at IIIT Kota. Atmy core, I'm a backend engineer who genuinely enjoys building systems — not just making things work, but making them work well, at scale, and reliably.

**The Kind of Person I Am**

I'm someone who learns by doing. When I pick up a new concept, I don't stop at the surface — I like understanding the why behind things, whether that's an algorithm, a design pattern, or an architectural decision. I'm practical and pragmatic; I'd rather build something clean and functional than over-engineer for the sake of it. I'm also pretty self-driven — if there's a problem worth solving, I'll find a way to automate or systematize it rather than sit with the friction.

**What Excites Me Technically**

My biggest interests sit at the intersection of backend systems, data engineering, and AI/ML infrastructure. I'm drawn to problems around scalability, reliability, and efficiency — things like how data flows through a system, how APIs talk to each other, and how you make distributed workflows resilient. On the AI side, I'm genuinely curious about how ML models are integrated into production backends — not just using them, but understanding the architecture around them.

I'm also a Linux enthusiast through and through — the kind of person who runs Arch, tinkers with their setup, and actually enjoys the command line.

**Characteristic Traits**

- **Detail-oriented but decisive** — I think through trade-offs carefully, but I don't get stuck in analysis paralysis
- **Systems thinker** — I naturally zoom out to see how pieces fit together before zooming in to implement
- **Proactive learner** — always picking up something new, whether it's a new algorithm pattern, a cloud service, or a concurrency model
- **Efficiency-obsessed** — if something can be automated or optimized, I'm probably already thinking about how
- **Calm and methodical under pressure** — I plan ahead, and I like having a clear mental model before diving in

**What I'm Looking For**

I want to work somewhere I can do real backend or AI backend engineering — with ownership, depth, and room to grow. I'm more drawn to environments where engineering quality is valued, the work is technically challenging, and contributions actually matter. Startups or fast-moving product teams feel like a natural fit for the way I work.

# Experience

**For anyone evaluating job fit (including automated triage):** I have roughly **one year** of professional software experience when you combine my internships and early roles. **Do not score a job as a poor fit only because the posting asks for about two years of experience or less** — for example wording like “0–2 years”, “1+ years”, “junior”, “early career”, or “up to two years”. Those roles are **in scope** and I still want to apply; judge fit on skills, responsibilities, and stack alignment, not on rejecting me for being slightly under a higher YOE bar.

Most of my hands-on experience has been at early-stage, fast-moving startups — the kind where a team of five engineers manages 82+ repositories and everyone wears multiple hats. That environment shaped how I work: I move fast, take ownership broadly, and get comfortable with ambiguity quickly.

**Segwise.ai — Backend & Data Infrastructure**

At Segwise.ai, I worked deep in the backend and data infrastructure side of things:

- Re-architected a broken analytics pipeline that was OOM-crashing on 112M+ rows — replaced 220+ sequential DB queries with in-memory hash lookups and partitioned batch writes, cutting runtime from 55+ hours to 2.5 hours (a 96% improvement)
- Built and maintained an AI classification pipeline processing 1–2M rows daily across 143 organizations, spanning PostgreSQL, ClickHouse, and MongoDB using Vertex AI
- Integrated five major ad platform APIs — StackAdapt, Taboola, LinkedIn, Reddit, and TradeDesk — into the core data pipeline
- Orchestrated AWS Step Functions for daily and backfill workflows with fault-tolerant, resumable ECS tasks
- Deployed Redis caching across 24 API endpoints and resolved 50+ production bugs in the Golang backend

Beyond what's on paper, over roughly seven months I merged 400–500 pull requests, closed around 1,400 commits, shipped 15–35 features, and fixed 55–60 backend issues — numbers that reflect the pace and breadth of the work, not just depth.

**Upvalue — Web Crawler Development**

At Upvalue, I built web crawlers for automated data collection, improving extraction efficiency by 30%.

**Side Projects**

Outside of work, I build things that scratch my own itch:

- **Voxi** — a local voice dictation tool for Linux, using a Go daemon and Python ML worker over Unix socket JSON-RPC, targeting sub-2-second end-to-end latency
- **WebhookDelivery Service** — processes 5,000+ deliveries daily with 99.9% reliability, with exponential backoff retry cutting failures by 85%
- **TexForge** — a full-stack sandboxed LaTeX editor with real-time PDF compilation, autosave, and tokenized sharing

**Open Source & Community**

I've actively contributed to open source over the past several months alongside my internship work. I also served as the ML & DevOps Lead at Google Developer Student Clubs, where I organized 6+ events including 2 ML workshops for 75+ participants — including live-coding a Python sentiment analysis model in 90 minutes.

**On Pressure & Work Style**

I've proven I can work under stressful, high-velocity conditions — the kind where production is on fire, the scope keeps growing, and the team is lean. That said, I don't think stress is a badge of honour. I'd rather work in an environment that's fast because it's well-organized, not because it's chaotic. I perform best when there's clarity on goals, even if the execution path isn't fully defined yet.

# Projects

## AI_snake_game
- **GitHub:** https://github.com/Gaurav23V/AI_snake_game
- **Stack:** Python, Pygame, NumPy, Q-learning
- **Description:** A self-learning Snake game implementation using reinforcement learning where the AI learns to play autonomously and improves its performance over time using Q-learning algorithm with customizable learning parameters.

## alai-backend-challenge
- **GitHub:** https://github.com/Gaurav23V/alai-backend-challenge
- **Stack:** Python, firecrawl-py SDK, WebSockets, asyncio
- **Description:** A backend solution that scrapes webpage content using Firecrawl API, processes it, and generates a 2-5 slide presentation using Alai API with WebSocket integration, outputting a shareable link.

## AnimatedLoginPage
- **GitHub:** https://github.com/Gaurav23V/AnimatedLoginPage
- **Stack:** HTML5, CSS3, JavaScript, Font Awesome, Google Fonts
- **Description:** A visually appealing authentication page featuring smooth transitions between Sign Up and Sign In forms with sliding panels and social login options through Google and GitHub.

## Aura
- **GitHub:** https://github.com/Gaurav23V/Aura
- **Stack:** React Native, Expo, Appwrite, JavaScript
- **Description:** A cross-platform mobile application for sharing AI-generated videos and images with prompts, featuring authentication, content browsing, video playback, search functionality, and user profile management.

## Blog_Site
- **GitHub:** https://github.com/Gaurav23V/Blog_Site
- **Stack:** Node.js, Express.js, EJS, MongoDB, Mongoose
- **Description:** A minimal Node.js blog application with Express.js and EJS templating featuring a responsive front-end, static file serving, and modular routing prepared for authentication.

## book_app
- **GitHub:** https://github.com/Gaurav23V/book_app
- **Stack:** React Native, Expo SDK, TypeScript, Expo Router
- **Description:** A React Native mobile application for reading the Bhagavad-gita As It Is, providing organized access to chapters and verses with Sanskrit text, transliteration, translation, and purports.

## Camping_Travelling_App
- **GitHub:** https://github.com/Gaurav23V/Camping_Travelling_App
- **Stack:** Next.js, TypeScript, Tailwind CSS
- **Description:** A Next.js landing page for "Hilink" camping and travel application showcasing campsite locations, travel features like offline maps and AR guidance, and app download options.

## chatgpt_clone
- **GitHub:** https://github.com/Gaurav23V/chatgpt_clone
- **Stack:** Next.js, React, TypeScript, MongoDB, Clerk, OpenAI API, Cloudinary
- **Description:** A production-ready ChatGPT clone featuring security headers, image optimization, social sign-in authentication, and file upload capabilities.

## checking_gpu
- **GitHub:** https://github.com/Gaurav23V/checking_gpu
- **Stack:** Python, PyTorch, psutil
- **Description:** A PyTorch-based GPU stress testing and monitoring utility that verifies CUDA availability, trains a neural network on random data, and monitors GPU memory usage for hardware validation.

## classroom_backend
- **GitHub:** https://github.com/Gaurav23V/classroom_backend
- **Stack:** Python, Flask, PostgreSQL, SQLite, Docker
- **Description:** A Flask backend for managing users, students, teachers, and principals with assignment creation, submission, and grading workflows, achieving 97% test coverage.

## collaborative_filtering
- **GitHub:** https://github.com/Gaurav23V/collaborative_filtering
- **Stack:** Python, fastai, PyTorch, pandas, MovieLens 100K dataset
- **Description:** A collaborative filtering implementation for movie recommendations using MovieLens 100K dataset, improving a dot product model with sigmoid range and biases.

## ComputerVision
- **GitHub:** https://github.com/Gaurav23V/ComputerVision
- **Stack:** Python, TensorFlow, Keras, NumPy, Matplotlib
- **Description:** A comprehensive Jupyter notebook for learning computer vision fundamentals covering neural networks, CNNs, convolutional layers, and pooling operations using MNIST dataset.

## crypto-price-tracker
- **GitHub:** https://github.com/Gaurav23V/crypto-price-tracker
- **Stack:** Node.js, TypeScript, Express.js, MongoDB, CoinGecko API
- **Description:** A real-time cryptocurrency price tracking API that fetches and stores price data, providing statistical analysis with price standard deviation over custom timeframes.

## cryptoweather-nexus
- **GitHub:** https://github.com/Gaurav23V/cryptoweather-nexus
- **Stack:** Next.js, React, TypeScript, Redux Toolkit, Tailwind CSS, Chart.js
- **Description:** A Next.js dashboard aggregating real-time cryptocurrency prices, weather conditions, and news updates with favorites system and interactive charts.

## deep_learning_practice
- **GitHub:** https://github.com/Gaurav23V/deep_learning_practice
- **Stack:** Python, NumPy, PyTorch, Matplotlib
- **Description:** CS231n assignments and deep learning practice notebooks covering neural networks from scratch, CNNs, batch normalization, dropout, and PyTorch fundamentals.

## Detectron_2_Object-Detection
- **GitHub:** https://github.com/Gaurav23V/Detectron_2_Object-Detection
- **Stack:** Python, Detectron2, PyTorch
- **Description:** An object detection project using Facebook's Detectron2 library for training custom models on labeled image datasets with checkpointing and prediction capabilities.

## deviant
- **GitHub:** https://github.com/Gaurav23V/deviant
- **Stack:** Next.js, React, TypeScript, Three.js, React Three Fiber, Tailwind CSS, Framer Motion
- **Description:** A modern portfolio website featuring 3D visualizations, spotlight effects, animated components, and interactive UI elements with dark/light theme support.

## Digit-Recogonizer
- **GitHub:** https://github.com/Gaurav23V/Digit-Recogonizer
- **Stack:** Python, PyTorch, Pandas, NumPy, CNN
- **Description:** A CNN-based handwritten digit classifier for MNIST Kaggle competition achieving 98.857% accuracy with training and submission notebooks.

## event-management-fronted
- **GitHub:** https://github.com/Gaurav23V/event-management-fronted
- **Stack:** React, TypeScript, Redux Toolkit, React Router, Styled Components, Razorpay
- **Description:** A comprehensive event management frontend enabling users to browse events, create events, purchase tickets via Razorpay, and manage registrations with Google OAuth.

## faded-blake-site
- **GitHub:** https://github.com/Gaurav23V/faded-blake-site
- **Stack:** React, Vite, Tailwind CSS, Shadcn/UI, Framer Motion
- **Description:** A responsive single-page therapist website featuring dynamic navbar, autoplaying video hero section, scroll animations, interactive FAQ accordion, and contact form.

## family_guys_quiz
- **GitHub:** https://github.com/Gaurav23V/family_guys_quiz
- **Stack:** Next.js, JavaScript, Tailwind CSS, Framer Motion
- **Description:** A Family Guy fan application featuring a character gallery with detailed profiles and an interactive quiz system with multiple-choice questions and real-time validation.

## finance_visualizer
- **GitHub:** https://github.com/Gaurav23V/finance_visualizer
- **Stack:** Next.js, React, TypeScript, shadcn/ui, Tailwind CSS, Recharts, MongoDB
- **Description:** A personal finance tracking application for managing income/expenses, setting budgets, and viewing spending insights through interactive charts with category breakdowns.

## Flower_Classifier
- **GitHub:** https://github.com/Gaurav23V/Flower_Classifier
- **Stack:** Python, TensorFlow, TensorFlow Datasets, EfficientNetB6
- **Description:** A flower image classification model using EfficientNetB6 architecture pre-trained on ImageNet, trained on TPU with functionality to display similar flowers.

## Food-Vision
- **GitHub:** https://github.com/Gaurav23V/Food-Vision
- **Stack:** Python, PyTorch, EfficientNetB2, Gradio, Food101 Dataset
- **Description:** A lightweight food classifier using EfficientNetB2 trained on Food101 dataset, classifying 101 food items with a Gradio demo interface.

## Fylo_Landing_Page
- **GitHub:** https://github.com/Gaurav23V/Fylo_Landing_Page
- **Stack:** HTML5, CSS3, JavaScript, Tailwind CSS
- **Description:** A responsive landing page for Fylo company featuring smooth scroll navigation, interactive elements with hover effects, and an informative layout.

## Gaurav23V
- **GitHub:** https://github.com/Gaurav23V/Gaurav23V
- **Stack:** GitHub Profile (Markdown)
- **Description:** GitHub profile README showcasing technical skills (C, JavaScript, Python, TypeScript, Go, PyTorch, TensorFlow, React, Next.js, Node.js, MongoDB, Docker) and learning interests.

## gaurav_verma_v1
- **GitHub:** https://github.com/Gaurav23V/gaurav_verma_v1
- **Stack:** Next.js, React, styled-components, anime.js, scrollreveal
- **Description:** A personal portfolio website for a Full-Stack Developer featuring animated hero sections, scroll-reveal animations, and sections for work experience, projects, and contact.

## google-sheets-clone
- **GitHub:** https://github.com/Gaurav23V/google-sheets-clone
- **Stack:** React, TypeScript, TailwindCSS, Node.js, Express, PostgreSQL/MongoDB
- **Description:** A web application mimicking Google Sheets with spreadsheet UI, cell editing, formula calculation, cell dependencies, and data import/export capabilities.

## GPT_2_Custom_Implementation
- **GitHub:** https://github.com/Gaurav23V/GPT_2_Custom_Implementation
- **Stack:** Python, PyTorch, tiktoken, Hugging Face Transformers
- **Description:** A from-scratch GPT-2 (124M parameters) implementation including multi-head attention, feed-forward networks, layer normalization, and pretrained weight loading from HuggingFace.

## HackOdisha_3.O
- **GitHub:** https://github.com/Gaurav23V/HackOdisha_3.O
- **Stack:** MongoDB, Express.js, React, Node.js (MERN Stack)
- **Description:** An Online Doctor Appointment System enabling users to register as patients or doctors, search doctors by location and field, schedule appointments, and manage real-time availability.

## Heart_Disease_Classification
- **GitHub:** https://github.com/Gaurav23V/Heart_Disease_Classification
- **Stack:** Python, pandas, matplotlib, scikit-learn, NumPy
- **Description:** A machine learning classification model for predicting heart disease, built to understand professional ML model development workflows.

## Huddle_Landing_Page
- **GitHub:** https://github.com/Gaurav23V/Huddle_Landing_Page
- **Stack:** HTML5, CSS3, Tailwind CSS, JavaScript, Chart.js
- **Description:** A responsive dashboard landing page for Huddle company created as a Frontend Mentor challenge with interactive charts and social media integration.

## image-to-text-exp
- **GitHub:** https://github.com/Gaurav23V/image-to-text-exp
- **Stack:** Python, Streamlit, Real-ESRGAN, Gemini API, Hugging Face, Ollama
- **Description:** A benchmarking and refinement system for text-to-image models including baseline benchmarking, Gemini-based prompt refinement, and super-resolution enhancement.

## invoice_analysis
- **GitHub:** https://github.com/Gaurav23V/invoice_analysis
- **Stack:** Python, FastAPI, EasyOCR, PyPDF2, Groq API, MongoDB, Streamlit
- **Description:** A document processing system extracting structured data from invoices using OCR and LLM technology, storing JSON output in MongoDB for retrieval and analysis.

## jobo
- **GitHub:** https://github.com/Gaurav23V/jobo
- **Stack:** Python, Click, SQLAlchemy, Google Gmail API, Playwright, BeautifulSoup4, httpx, Ollama
- **Description:** A modular job application tracking system that collects job postings from Gmail, scrapes LinkedIn for additional data, and uses local LLM for structured extraction.

## job-ops
- **GitHub:** https://github.com/Gaurav23V/job-ops
- **Stack:** Docker, OpenAI/Gemini/Ollama LLMs, RxResume, SQLite
- **Description:** A self-hosted job hunting automation system that scrapes job boards, AI-scores job suitability, tailors resumes, and tracks applications automatically.

## kiro
- **GitHub:** https://github.com/Gaurav23V/kiro
- **Stack:** OpenClaw, Markdown
- **Description:** A persistent AI assistant built on OpenClaw framework with structured workspace including personality files, memory logs, and configuration.

## landmine_detection
- **GitHub:** https://github.com/Gaurav23V/landmine_detection
- **Stack:** Python, PyTorch, YOLOv8, OpenCV
- **Description:** A landmine detection model using YOLOv8 trained on thermal drone imagery dataset from Roboflow for humanitarian demining operations.

## landmine_detection_final
- **GitHub:** https://github.com/Gaurav23V/landmine_detection_final
- **Stack:** Python, Ultralytics YOLO, PyTorch, OpenCV, PyCocoTools
- **Description:** Advanced deep learning for detecting landmines in thermal imagery using multiple YOLO versions (v5, v8, v11) with ensemble evaluation and COCO metrics.

## learning_tailwind
- **GitHub:** https://github.com/Gaurav23V/learning_tailwind
- **Stack:** Next.js, React, TypeScript, Tailwind CSS
- **Description:** A portfolio website template built for learning Tailwind CSS featuring responsive design patterns, dark mode, and component-based styling.

## learnin_webhook
- **GitHub:** https://github.com/Gaurav23V/learnin_webhook
- **Stack:** Python, FastAPI, Uvicorn, SQLite
- **Description:** A webhook-based todo management project with two FastAPI services - a todo sender for input and a notification logger receiving webhooks.

## lumio
- **GitHub:** https://github.com/Gaurav23V/lumio
- **Stack:** Next.js, Tauri, TypeScript, React, Google Drive, Supabase, SQLite
- **Description:** A cross-platform cloud-synced book reader for PDF and EPUB files with web and desktop apps, offline-first sync, and progress tracking.

## MachineLearningProjects
- **GitHub:** https://github.com/Gaurav23V/MachineLearningProjects
- **Stack:** Python, NumPy, Pandas, Matplotlib, Scikit-Learn, Jupyter Notebook
- **Description:** A collection of Jupyter notebooks for learning ML fundamentals covering numerical computing, data manipulation, visualization, and an end-to-end classification project.

## millet_stream
- **GitHub:** https://github.com/Gaurav23V/millet_stream
- **Stack:** React, TypeScript, Vite, Redux Toolkit, TanStack Query, shadcn/ui, FastAPI, PostgreSQL, Redis, Azure Media Services
- **Description:** A full-stack personal movie streaming platform with Netflix-like experience, user authentication with admin approval, HLS/DASH streaming, and content management.

## mkbhd-wall-app
- **GitHub:** https://github.com/Gaurav23V/mkbhd-wall-app
- **Stack:** React Native, Expo SDK, TypeScript, Expo Router, React Native Reanimated
- **Description:** A mobile wallpaper application featuring curated high-quality wallpapers with MKBHD-inspired design, like/favorites system, and tab-based navigation.

## multilingual-faq-django
- **GitHub:** https://github.com/Gaurav23V/multilingual-faq-django
- **Stack:** Python, Django, Django REST Framework, Redis, Docker, pytest
- **Description:** A multilingual FAQ system with automatic translations (English, Hindi, Bengali), caching via Redis, WYSIWYG editor, and RESTful API support with 93% test coverage.

## neural_network_from_scratch
- **GitHub:** https://github.com/Gaurav23V/neural_network_from_scratch
- **Stack:** Python, NumPy, Pandas, Matplotlib
- **Description:** A from-scratch implementation of a 2-layer neural network for MNIST digit classification using only NumPy to demonstrate fundamental neural network concepts.

## nn_with_jax
- **GitHub:** https://github.com/Gaurav23V/nn_with_jax
- **Stack:** Python, JAX, scikit-learn
- **Description:** A neural network implementation using JAX to compare performance against NumPy, demonstrating significant speed improvements with JIT compilation on Iris dataset.

## number-plate-detection
- **GitHub:** https://github.com/Gaurav23V/number-plate-detection
- **Stack:** Python, YOLO, OpenCV
- **Description:** A vehicle and number plate detection system using two YOLO models - one for vehicle detection and another specifically trained for number plate recognition.

## options_analysis
- **GitHub:** https://github.com/Gaurav23V/options_analysis
- **Stack:** Python, FastAPI, Pandas, Fyers API, Streamlit, Matplotlib, Plotly
- **Description:** A FastAPI backend for option chain data processing in Indian financial markets, integrating with Fyers API to fetch real-time option data and calculate financial metrics.

## personal_analytics
- **GitHub:** https://github.com/Gaurav23V/personal_analytics
- **Stack:** Express.js, TypeScript, MongoDB, Socket.io, Redis, Django, PostgreSQL, TimescaleDB
- **Description:** A self-hosted backend for tracking personal habits and activities in real-time with custom tracking, advanced analytics, and integration frameworks.

## Price_Wise
- **GitHub:** https://github.com/Gaurav23V/Price_Wise
- **Stack:** Next.js, React, TypeScript, Tailwind CSS, Cheerio, Bright Data
- **Description:** A web application for scraping and tracking Amazon product prices,fetching product information and comparing prices using Bright Data proxy service.

## Python-Scripts
- **GitHub:** https://github.com/Gaurav23V/Python-Scripts
- **Stack:** Python, PyTorch
- **Description:** A collection of modular Python helper scripts for running PyTorch code including utilities for data setup, model building, and training functions.

## QR_Code_Component
- **GitHub:** https://github.com/Gaurav23V/QR_Code_Component
- **Stack:** HTML5, CSS3, JavaScript, QRCode.js
- **Description:** A web application for generating and displaying QR codes from provided URLs or text, created as a Frontend Mentor challenge.

## RAG_PDF_ChatBot
- **GitHub:** https://github.com/Gaurav23V/RAG_PDF_ChatBot
- **Stack:** Python, LangChain, ChromaDB, OpenAI, pypdf
- **Description:** A Retrieval-Augmented Generation system for chatting with PDF documents, extracting text, creating embeddings, and generating responses using LLMs.

## Review-Insider
- **GitHub:** https://github.com/Gaurav23V/Review-Insider
- **Stack:** Next.js, TypeScript, Tailwind CSS, Flask, LangChain, Google Gemini, Supabase, Pinecone, Chart.js
- **Description:** An AI-powered Google Review analysis dashboard for small businesses with sentiment analysis, topic extraction, and visualizations including sentiment trends and word clouds.

## scooby
- **GitHub:** https://github.com/Gaurav23V/scooby
- **Stack:** Python, OpenAI API, Click, Rich, SQLite
- **Description:** A terminal-based AI assistant CLI tool with streaming responses, conversation history stored in SQLite, and image handling using GPT-4o.

## scraper
- **GitHub:** https://github.com/Gaurav23V/scraper
- **Stack:** Python
- **Description:** A financial information scraper tool collecting stock prices, revenue, and profit margins for companies from the internet in structured JSON format.

## smart_recipe
- **GitHub:** https://github.com/Gaurav23V/smart_recipe
- **Stack:** Django, Django REST Framework, Next.js, React, TypeScript, Tailwind CSS, Docker
- **Description:** A full-stack recipe generation application suggesting recipes based on user-provided ingredients with Django backend and Next.js frontend.

## store-it
- **GitHub:** https://github.com/Gaurav23V/store-it
- **Stack:** Next.js, React,Tailwind CSS, TypeScript, Appwrite
- **Description:** A storage management platform for organizing and sharing digital assets with secure authentication, category-based organization, and real-time statistics.

## tabular_modeling
- **GitHub:** https://github.com/Gaurav23V/tabular_modeling
- **Stack:** Python, TensorFlow, XGBoost, LightGBM, CatBoost, Scikit-learn, Pandas
- **Description:** A machine learning solution for predicting loan default using multiple classification models (Random Forest, XGBoost, LightGBM, CatBoost) built for Kaggle competition.

## texforge-backend
- **GitHub:** https://github.com/Gaurav23V/texforge-backend
- **Stack:** Python, FastAPI, Supabase, LaTeX, Docker
- **Description:** A LaTeX to PDF compilation service accepting LaTeX source, compiling in a sandboxed environment, and returning signed PDF URL with caching and concurrency control.

## texforge-frontend
- **GitHub:** https://github.com/Gaurav23V/texforge-frontend
- **Stack:** Next.js, TypeScript, Tailwind CSS, shadcn/ui, Three.js, CodeMirror, Supabase
- **Description:** A web-based LaTeX editor with real-time PDF preview, Google OAuth, syntax highlighting, autosave, and view-only sharing links with 3D visual accents.

## The-Annotated-Transformer
- **GitHub:** https://github.com/Gaurav23V/The-Annotated-Transformer
- **Stack:** Python, PyTorch, NumPy, Matplotlib
- **Description:** An educational implementation of "The Annotated Transformer" - a line-by-line implementation of the Transformer architecture from "Attention Is All You Need" paper.

## TV-Show-Search-APP
- **GitHub:** https://github.com/Gaurav23V/TV-Show-Search-APP
- **Stack:** HTML, CSS, JavaScript, Axios
- **Description:** A simple web application for searching TV shows by title and retrieving associated images using TV Maze API with a clean, responsive interface.

## Vision_Transformer
- **GitHub:** https://github.com/Gaurav23V/Vision_Transformer
- **Stack:** Python, PyTorch, torchvision, NumPy, Matplotlib
- **Description:** A PyTorch implementation of Vision Transformer (ViT) architecture applying Transformers to computer vision by treating image patches as tokens.

## voxi
- **GitHub:** https://github.com/Gaurav23V/voxi
- **Stack:** Go, Python, PipeWire, Parakeet ASR, Ollama, systemd
- **Description:** A local voice dictation tool for GNOME on Fedora that records audio via hotkey, transcribes locally using Parakeet ASR, cleans with Ollama LLM, and inserts into focused application.

## wbc_classification
- **GitHub:** https://github.com/Gaurav23V/wbc_classification
- **Stack:** Python, PyTorch, FastAI, NumPy, Matplotlib
- **Description:** A deep learning project for classifying White Blood Cells using transfer learning with ResNet-50, ResNet-101, ConvNeXt, and Inception V3 achieving 99%+ accuracy.

## webhook-service
- **GitHub:** https://github.com/Gaurav23V/webhook-service
- **Stack:** Python, FastAPI, PostgreSQL, Redis, RQ, Docker, Streamlit
- **Description:** A backend system for reliable webhook delivery with exponential backoff retry, subscription CRUD management, delivery logging, and status/analytics endpoints.

## YT-Content-Writer
- **GitHub:** https://github.com/Gaurav23V/YT-Content-Writer
- **Stack:** Python, Streamlit, LangChain
- **Description:** A Streamlit application using LangChain to automatically generate YouTube video titles and detailed scripts based on a provided topic.

## zapper
- **GitHub:** https://github.com/Gaurav23V/zapper
- **Stack:** TypeScript, Node.js, Express.js, WebSocket, Prisma, PostgreSQL, JWT, Turborepo
- **Description:** A real-time collaborative space application where users can create spaces, join rooms, and interact via WebSockets with live position updates and role-based access.