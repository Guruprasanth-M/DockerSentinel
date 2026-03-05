FROM nginx:1.25-alpine

# Install curl for health checks
RUN apk add --no-cache curl

# Copy nginx configuration
COPY nginx.conf /etc/nginx/nginx.conf

# Copy static files
COPY static/ /usr/share/nginx/html/

# Ensure nginx cache dirs are writable by non-root
RUN mkdir -p /var/cache/nginx/client_temp /var/cache/nginx/proxy_temp \
    /var/cache/nginx/fastcgi_temp /var/cache/nginx/uwsgi_temp \
    /var/cache/nginx/scgi_temp && \
    chown -R nginx:nginx /var/cache/nginx

# Run as non-root user (port 8080 > 1024, no root needed)
USER nginx

EXPOSE 8080

CMD ["nginx", "-g", "daemon off;"]
