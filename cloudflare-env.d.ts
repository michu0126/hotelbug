declare namespace Cloudflare {
  interface Env {
    DB?: D1Database;
    BUCKET?: R2Bucket;
    TELEGRAM_BOT_TOKEN?: string;
    TELEGRAM_CHAT_ID?: string;
    HOTEL_RATE_API_URL?: string;
    HOTEL_RATE_API_KEY?: string;
  }
}
