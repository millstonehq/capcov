import { chromium } from "playwright";

const base = process.env.BASE_URL;
const browser = await chromium.launch({ channel: process.env.PW_CHANNEL || undefined });
const context = await browser.newContext({ recordHar: { path: "e2e/run.har" } });
const page = await context.newPage();
await page.goto(base + "/notes");
await page.evaluate(async (base) => {
  await fetch(base + "/notes", { method: "POST", body: new URLSearchParams({ title: "ship it" }) });
}, base);
await page.goto(base + "/notes");
await context.close();
await browser.close();
