/* End-to-end browser test: real HTTP server + disposable SQLite; no API mocks. */
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const { mkdtempSync, rmSync } = require("node:fs");
const { tmpdir } = require("node:os");
const { join } = require("node:path");
const { createInterface } = require("node:readline");
async function main() {
  const temp = mkdtempSync(join(tmpdir(), "clinic-browser-"));
  const child = spawn(
    process.env.PYTHON || "python",
    ["tests/browser_server.py", join(temp, "clinic.db")],
    { stdio: ["ignore", "pipe", "inherit"] },
  );
  let browser;
  try {
    const config = await new Promise((resolve, reject) => {
      const timer = setTimeout(
        () => reject(Error("Server did not start")),
        10000,
      );
      createInterface({ input: child.stdout }).once("line", (line) => {
        clearTimeout(timer);
        try {
          resolve(JSON.parse(line));
        } catch (e) {
          reject(e);
        }
      });
      child.once("exit", (code) => {
        if (code) reject(Error("Server exited " + code));
      });
    });
    const base = `http://127.0.0.1:${config.port}`;
    browser = await chromium.launch({
      headless: true,
      args: ["--no-sandbox"],
      ...(process.env.BROWSER_EXECUTABLE
        ? { executablePath: process.env.BROWSER_EXECUTABLE }
        : {}),
    });
    const page = await browser.newPage({
      viewport: { width: 1440, height: 1100 },
    });
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await page.goto(base);
    await page.locator("[data-doctor]").first().waitFor();
    await page.screenshot({ path: join(temp, "desktop.png"), fullPage: true });
    await page.getByRole("button", { name: /Dr. Maya Patel/ }).click();
    await page.locator("#book-day").fill(config.day);
    await page.locator("#book-day").dispatchEvent("change");
    await page.waitForFunction(
      () => document.querySelector("#slots button")?.disabled === false,
    );
    await page.locator("[data-slot]").first().click();
    await page.locator("#book-count").selectOption("2");
    await page.locator("#patient-name").fill("Browser Patient");
    await page.locator("#patient-phone").fill("+14155550123");
    await page.locator("#review-booking").click();
    await page.locator("#hold-review").waitFor({ state: "visible" });
    assert.equal(await page.locator("#hold-summary p.hint").count(), 2);
    await page.locator("#confirm-booking").click();
    await page.locator("#booking-result").waitFor({ state: "visible" });
    assert.match(
      await page.locator("#booking-result").innerText(),
      /You’re booked/,
    );
    await page.locator("#booking-result [data-save-reference]").click();
    await page
      .locator("#patient-appointments .appointment-row")
      .first()
      .waitFor();
    assert.equal(
      await page.locator("#patient-appointments .appointment-row").count(),
      2,
    );
    await page.locator("#patient-appointments [data-cancel]").last().click();
    await page.locator("#confirm-yes").click();
    await page.waitForFunction(
      () =>
        document.querySelectorAll("#patient-appointments .status.cancelled")
          .length === 1,
    );
    await page.locator("#patient-appointments [data-move]").click();
    await page
      .locator("#move-slot option")
      .first()
      .waitFor({ state: "attached" });
    await page.locator("#move-form .primary").click();
    await page.locator("#move-dialog").waitFor({ state: "hidden" });
    await page.locator("#cancel-series").click();
    await page.locator("#confirm-yes").click();
    await page.waitForFunction(
      () =>
        document.querySelectorAll("#patient-appointments .status.cancelled")
          .length === 2,
    );
    assert.match(await page.locator("#booking-result").innerText(), /cancelled/);
    console.log(
      "PASS patient: recurring booking → partial cancel → move → series cancel",
    );
    await page.locator('[data-view="call"]').click();
    await page.locator("#start-call").click();
    for (const text of [
      "book",
      "1",
      config.day,
      "1",
      "2",
      "Voice Patient",
      "+14155550123",
      "yes",
    ]) {
      await page.locator("#call-text").fill(text);
      const response = page.waitForResponse((r) =>
        r.url().endsWith("/api/call/turn"),
      );
      await page.locator("#call-send").click();
      await response;
      await page.waitForFunction(
        () =>
          document.querySelector("#call-send").disabled === false ||
          document.querySelector("#call-status").textContent ===
            "Call complete",
      );
    }
    assert.match(
      await page.locator("#transcript").innerText(),
      /Booked 2 appointment/,
    );
    await page.locator("#call-result").waitFor({ state: "visible" });
    assert.equal(await page.locator("#call-text").isDisabled(), true);
    console.log(
      "PASS call: live slots → weekly hold → explicit confirmation → credentials",
    );
    await page.locator("#staff-switch").click();
    await page.locator("#login-username").fill("browser-admin");
    await page.locator("#login-password").fill("browser-test-password-123");
    await page.locator("#login-form .primary").click();
    await page.locator("#staff-control").waitFor({ state: "visible" });
    await page
      .locator("#staff-appointment-list .appointment-row")
      .first()
      .waitFor();
    assert.equal(
      await page.locator("#staff-appointment-list .appointment-row").count(),
      4,
    );
    await page.locator('[data-staff="availability"]').click();
    await page.locator("#availability-doctor").selectOption("maya-patel");
    await page.waitForFunction(() =>
      document.querySelector("#availability-windows").value.includes("09:00"),
    );
    await page.locator("#availability-type").selectOption("override");
    await page.locator("#availability-day").fill(config.override_day);
    await page.locator("#availability-windows").fill("");
    const availResponse = page.waitForResponse(
      (r) =>
        r.url().endsWith("/api/admin/availability") &&
        r.request().method() === "POST",
    );
    await page.locator("#availability-form .primary").click();
    assert.equal((await availResponse).status(), 200);
    const slots = await page.request.get(
      base + `/api/slots?doctor_id=maya-patel&day=${config.override_day}`,
    );
    assert.equal((await slots.json()).slots.length, 0);
    await page.locator('[data-staff="admin"]').click();
    await page.locator("#user-username").fill("created-manager");
    await page.locator("#user-password").fill("manager-password-123");
    await page.locator("#user-form .primary").click();
    await page.waitForFunction(() =>
      document
        .querySelector("#user-list")
        .textContent.includes("created-manager"),
    );
    await page.locator("#doctor-id").fill("alex-morgan");
    await page.locator("#doctor-name").fill("Dr. Alex Morgan");
    await page.locator("#doctor-specialty").fill("Family medicine");
    await page.locator("#doctor-form .primary").click();
    await page.waitForFunction(() =>
      document
        .querySelector("#doctor-admin-list")
        .textContent.includes("Alex Morgan"),
    );
    await page.locator("#logout").click();
    await page.locator("#staff-login").waitFor({ state: "visible" });
    await page.locator("#login-username").fill("created-manager");
    await page.locator("#login-password").fill("manager-password-123");
    await page.locator("#login-form .primary").click();
    await page.locator("#staff-control").waitFor({ state: "visible" });
    assert.equal(await page.locator("#admin-tab").isVisible(), false);
    await page.locator("#staff-appointment-list [data-cancel]").first().click();
    await page.locator("#confirm-yes").click();
    await page.waitForFunction(
      () =>
        document.querySelectorAll("#staff-appointment-list .status.cancelled")
          .length === 3,
    );
    console.log(
      "PASS staff: appointment list → time off → create doctor/staff → manager permissions → cancel",
    );
    await page.setViewportSize({ width: 390, height: 844 });
    await page.locator("#staff-switch").click();
    await page.locator('[data-view="book"]').click();
    assert.equal(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
      true,
    );
    await page.screenshot({ path: join(temp, "mobile.png"), fullPage: true });
    assert.deepEqual(errors, []);
    if (process.env.SCREENSHOT_DIR) {
      const { copyFileSync, mkdirSync } = require("node:fs");
      mkdirSync(process.env.SCREENSHOT_DIR, { recursive: true });
      for (const n of ["desktop", "mobile"])
        copyFileSync(
          join(temp, n + ".png"),
          join(process.env.SCREENSHOT_DIR, n + ".png"),
        );
    }
    console.log("PASS mobile layout; no browser JavaScript errors");
  } finally {
    if (browser) await browser.close();
    child.kill();
    await new Promise((resolve) => child.once("exit", resolve));
    rmSync(temp, { recursive: true, force: true });
  }
}
main().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
