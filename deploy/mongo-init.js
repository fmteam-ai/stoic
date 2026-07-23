// Runs once on first MongoDB init (as root, via docker-entrypoint-initdb.d).
// Creates the least-privilege application account: readWrite on DB_NAME only.
// The app password comes from a Docker secret, never from an env var.
const fs = require("fs");
const dbName = process.env.DB_NAME;
const appUser = process.env.MONGO_APP_USER;
const appPwd = fs.readFileSync("/run/secrets/mongo_app_password", "utf8").trim();
if (!dbName || !appUser || !appPwd) {
  throw new Error("DB_NAME / MONGO_APP_USER / mongo_app_password secret must be set");
}
const appDb = db.getSiblingDB(dbName);
appDb.createUser({
  user: appUser,
  pwd: appPwd,
  roles: [{ role: "readWrite", db: dbName }],
});
print(`created app user '${appUser}' with readWrite on '${dbName}'`);
