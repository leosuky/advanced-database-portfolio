// mongosh script - run AFTER `python mongo_demo.py --uri mongodb://localhost:27017` has loaded the data.
//   mongosh "mongodb://localhost:27017/dataxi" 02_queries_and_explain.js
db = db.getSiblingDB("dataxi");

print("--- M1 point read");
printjson(db.match_documents.findOne({ _id: 951 }, { shots: 0, "home.lineup": 0, "away.lineup": 0 }));

print("--- M2 fixtures with execution stats (expect IXSCAN, not COLLSCAN)");
const ex = db.match_documents.find({ "home.team_id": 1, season: "2025/26" }).sort({ matchday: -1 }).explain("executionStats");
// Walk the whole winning plan and list every stage (the shape differs between MongoDB versions/engines).
function stages(node, acc = []) {
  if (!node || typeof node !== "object") return acc;
  if (node.stage) acc.push(node.stage + (node.indexName ? "(" + node.indexName + ")" : ""));
  for (const k of ["queryPlan", "inputStage"]) if (node[k]) stages(node[k], acc);
  if (Array.isArray(node.inputStages)) node.inputStages.forEach(n => stages(n, acc));
  return acc;
}
print("plan stages: " + stages(ex.queryPlanner.winningPlan).join(" <- "));
print("docsExamined=" + ex.executionStats.totalDocsExamined + " returned=" + ex.executionStats.nReturned + " ms=" + ex.executionStats.executionTimeMillis);

print("--- M3 top scorers (aggregation)");
printjson(db.match_documents.aggregate([
  { $match: { season: "2025/26" } },
  { $project: { players: { $concatArrays: ["$home.lineup", "$away.lineup"] } } },
  { $unwind: "$players" },
  { $group: { _id: { id: "$players.player_id", name: "$players.name" }, goals: { $sum: "$players.goals" } } },
  { $sort: { goals: -1 } }, { $limit: 5 }
]).toArray());

print("--- M4 stoppage-time goals");
print(db.match_documents.countDocuments({ shots: { $elemMatch: { minute: { $gt: 90 }, outcome: "goal" } } }));

print("--- same query without the index hint-free planner: forced collection scan for comparison");
const noIdx = db.match_documents.find({ "home.team_id": 1, season: "2025/26" }).hint({ $natural: 1 }).explain("executionStats");
print("COLLSCAN docsExamined=" + noIdx.executionStats.totalDocsExamined + " ms=" + noIdx.executionStats.executionTimeMillis);
