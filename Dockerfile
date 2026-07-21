# Dockerfile for Survey Co-Pilot API
FROM python:3.10-slim

# System deps + R
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    r-base \
    r-base-dev \
    libcurl4-openssl-dev \
    libssl-dev \
    libxml2-dev \
    libv8-dev \
    pandoc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install R packages required by R wrappers
RUN Rscript -e 'options(repos="https://cloud.r-project.org"); install.packages(c("psych","magrittr","seminr"), dependencies=TRUE)'

# Copy app
COPY . .

# Environment
ENV PYTHONUNBUFFERED=1
EXPOSE 8000

# Default start command
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
