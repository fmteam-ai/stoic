// Runs once on first MongoDB init (as root, via docker-entrypoint-initdb.d).
// Creates the least-privilege application account: readWrite on DB_NAME only.
const dbName = process.env.DB_NAME;
const appUser = process.env.MONGO_APP_USER;
const appPwd = process.env.MONGO_APP_PASSWORD;
if (!dbName || !appUser || !appPwd) {
  throw new Error("DB_NAME / MONGO_APP_USER / MONGO_APP_PASSWORD must be set");
}
const appDb = db.getSiblingDB(dbName);
appDb.createUser({
  user: appUser,
  pwd: appPwd,
  roles: [{ role: "readWrite", db: dbName }],
});
print(`created app user '${appUser}' with readWrite on '${dbName}'`);
