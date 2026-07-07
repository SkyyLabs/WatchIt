# WatchIt guardian dashboard (Next.js + Clerk).
# NEXT_PUBLIC_* values are baked into the client bundle at build time (they are
# public by design). CLERK_SECRET_KEY is runtime-only — pass it at `docker run`.
#   docker build -f apps/dashboard/Dockerfile \
#     --build-arg NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=pk_live_... \
#     --build-arg NEXT_PUBLIC_API_BASE_URL=https://api.example.com apps/dashboard
FROM node:20-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
ARG NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY
ARG NEXT_PUBLIC_API_BASE_URL
ENV NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=$NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY \
    NEXT_PUBLIC_API_BASE_URL=$NEXT_PUBLIC_API_BASE_URL \
    CLERK_SECRET_KEY=sk_test_build_placeholder
RUN npm run build

FROM node:20-slim
WORKDIR /app
ENV NODE_ENV=production
COPY --from=build /app/package.json /app/package-lock.json ./
COPY --from=build /app/node_modules ./node_modules
COPY --from=build /app/.next ./.next
COPY --from=build /app/next.config.js ./
EXPOSE 4848
CMD ["npm", "run", "start"]
