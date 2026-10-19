// Railway Infrastructure as Code для бота регистрации.
// railway.json (Config as Code) Railway объявил устаревшим и не применяет к новым сервисам,
// поэтому настройки деплоя описаны здесь. Применение: см. README, раздел «Railway».
//
// Проект общий с ботом игр (bchbot), поэтому файл — именованный partial:
// он управляет только своими ресурсами и не трогает сервисы бота игр.
import { defineRailway, github, postgres, preserve, project, redis, service } from "railway/iac";

export const partial = "bc2regbot";

export default defineRailway(() => {
  const db = postgres("bc-postgres");
  const cache = redis("bc-redis");

  const bot = service("bc2regbot", {
    source: github("ewalovespewa-cmyk/bc2regbot", { branch: "main" }),
    // Сборка по Dockerfile из корня репозитория; миграции — до переключения трафика.
    preDeploy: "alembic upgrade head",
    healthcheck: "/health",
    healthcheckTimeout: 120,
    // Ровно один инстанс: webhook, фоновые воркеры и счётчики рассчитаны на один процесс.
    replicas: 1,
    env: {
      DATABASE_URL: db.env.DATABASE_URL,
      REDIS_URL: cache.env.REDIS_URL,
      BOT_TOKEN: preserve(),
      WEBHOOK_SECRET: preserve(),
      WEBHOOK_BASE_URL: preserve(),
      SUPERADMIN_IDS: preserve(),
      ADMIN_CHAT_ID: preserve(),
    },
  });

  return project("bchbot", { resources: [db, cache, bot] });
});
