FROM node:24.18.0-alpine AS build
WORKDIR /ui
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:1.29.3-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /ui/dist /usr/share/nginx/html
