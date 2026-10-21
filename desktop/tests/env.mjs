// The backend under test never sees this machine's keys — not .env, not the keychain, not WB_SECRET_* from the
// shell — so no test can reach a real data source, model or Feishu bot. A test that needs a key sets its own.
import path from "node:path"

export const isolated = (dataDir, extra = {}) => ({
  ...Object.fromEntries(Object.entries(process.env).filter(([k]) => !k.startsWith("WB_SECRET_"))),
  WB_DATA_DIR: dataDir, WB_ENV_FILE: path.join(dataDir, "none.env"), PYTHON_KEYRING_BACKEND: "keyring.backends.null.Keyring",
  WB_USER_DATA: path.join(dataDir, "electron"),   // never your own app's profile
  ...extra,
})
