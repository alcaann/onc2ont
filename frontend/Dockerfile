# --- START OF FILE frontend/Dockerfile ---

# ---- Build Stage ----
# Use an official Node.js LTS Alpine image for building the frontend
FROM node:lts-alpine AS builder

# Set working directory
WORKDIR /app

# Copy package files first for cache optimization
COPY package.json package-lock.json* ./

# Install dependencies cleanly using lock file
RUN npm ci

# Copy the rest of the frontend source code
COPY . .

# Run the build command (defined in package.json)
# This should create a 'dist' directory
RUN npm run build

# ---- Production Stage ----
# Use the official Nginx Alpine image
FROM nginx:stable-alpine

# Remove default Nginx configuration
RUN rm /etc/nginx/conf.d/default.conf

# Copy the custom Nginx configuration file (we will create this next)
# This file MUST exist in the same directory as this Dockerfile (i.e., frontend/)
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Copy the built static assets from the 'builder' stage
# Assumes 'npm run build' outputs to '/app/dist' in the builder stage
COPY --from=builder /app/dist /usr/share/nginx/html

# Expose port 80 (Nginx default)
EXPOSE 80

# Default command to start Nginx in the foreground
CMD ["nginx", "-g", "daemon off;"]

# --- END OF FILE frontend/Dockerfile ---