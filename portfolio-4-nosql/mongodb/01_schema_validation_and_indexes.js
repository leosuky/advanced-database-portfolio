// mongosh script - run AFTER `python mongo_demo.py --uri mongodb://localhost:27017` has loaded the data:
//   mongosh "mongodb://localhost:27017/dataxi" 01_schema_validation_and_indexes.js
db = db.getSiblingDB("dataxi");

// Optional schema enforcement: MongoDB is "schema-flexible", not "schema-free".
// The match page documents get a validator; the raw provider payloads deliberately do not.
// mongo_demo.py loads the data first, so the collection may already exist: then attach the validator
// with collMod instead of createCollection (which would fail with "collection already exists").
const matchDocOptions = {
  validator: { $jsonSchema: {
    bsonType: "object",
    required: ["_id", "season", "matchday", "kickoff", "home", "away"],
    properties: {
      _id:      { bsonType: "int" },
      season:   { bsonType: "string", pattern: "^[0-9]{4}/[0-9]{2}$" },
      matchday: { bsonType: "int", minimum: 1, maximum: 60 },
      home: { bsonType: "object", required: ["team_id", "name", "goals", "lineup"],
              properties: { goals: { bsonType: "int", minimum: 0 }, lineup: { bsonType: "array", maxItems: 25 } } },
      away: { bsonType: "object", required: ["team_id", "name", "goals", "lineup"],
              properties: { goals: { bsonType: "int", minimum: 0 }, lineup: { bsonType: "array", maxItems: 25 } } }
    } } },
  validationLevel: "strict", validationAction: "error"
};
if (db.getCollectionNames().includes("match_documents")) {
  printjson(db.runCommand(Object.assign({ collMod: "match_documents" }, matchDocOptions)));
} else {
  db.createCollection("match_documents", matchDocOptions);
}

db.match_documents.createIndex({ "home.team_id": 1, season: 1 });
db.match_documents.createIndex({ "away.team_id": 1, season: 1 });
db.match_documents.createIndex({ "shots.player_id": 1 });                      // multikey index (array field)
db.provider_raw_payloads.createIndex({ match_id: 1, provider: 1 }, { unique: true });

print("--- the validator rejects a bad document:");
try { db.match_documents.insertOne({ _id: NumberInt(-1), season: "bad", matchday: NumberInt(99) }); }
catch (e) {
  // a write error carries the numeric code (121 = DocumentValidationFailure) but not always codeName
  print("REJECTED: code " + e.code + " (" + (e.codeName || "DocumentValidationFailure if code is 121") + ")");
  print("message: " + e.message);
}

printjson(db.match_documents.getIndexes().map(i => i.name));
