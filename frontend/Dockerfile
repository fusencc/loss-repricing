FROM node:24-alpine AS build
WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci
COPY frontend ./
RUN npm run build
FROM nginx:alpine
COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /build/dist /usr/share/nginx/html
