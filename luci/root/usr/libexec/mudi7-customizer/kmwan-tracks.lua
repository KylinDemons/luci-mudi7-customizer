local cursor = require("uci").cursor("/etc/config", arg[1])
local addresses = {
    ["4"] = { arg[2], arg[3], arg[4], arg[5] },
    ["6"] = { arg[6], arg[7], arg[8], arg[9] }
}

if not cursor:load("kmwan") or not cursor:get_all("glconfig", "general") then
    os.exit(2)
end
-- A UCI commit can also persist changes from the shared delta directory.
if next(cursor:changes("kmwan")) or next(cursor:changes("glconfig")) then
    os.exit(3)
end

local members = {}
cursor:foreach("kmwan", "member", function(member)
    members[#members + 1] = member
end)
if #members == 0 then
    os.exit(2)
end

for _, member in ipairs(members) do
    local targets = addresses[member.addr_type]
    if not targets then
        os.exit(2)
    end
    -- Firmware uses one probing command per member. Keep the configured command.
    local first = type(member.tracks) == "table" and member.tracks[1] or member.tracks
    local method = type(first) == "string" and first:match("^([^,]+),") or "ping"
    local tracks = {}
    for index, address in ipairs(targets) do
        tracks[index] = method .. "," .. address
    end
    assert(cursor:set("kmwan", member[".name"], "tracks", tracks))
end

-- The firmware seeds newly created or reconfigured modem members from these lists.
assert(cursor:set("glconfig", "general", "track_ip", addresses["4"]))
assert(cursor:set("glconfig", "general", "track_ipv6", addresses["6"]))
assert(cursor:save("glconfig"))
assert(cursor:save("kmwan"))
assert(cursor:commit("glconfig"))
assert(cursor:commit("kmwan"))
