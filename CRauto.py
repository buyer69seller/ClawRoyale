import asyncio
import json
import logging
import requests
import websockets

# Konfigurasi Endpoint & Bot
AGENT_NAME = "buy6_9sell"
BASE_API_URL = "https://cdn.clawroyale.ai/api"
WS_JOIN_URL = "wss://cdn.clawroyale.ai/ws/join"
API_KEY = "mr_live_5uFKB4CBSSaWNpbxpWaiyOteqg3JRhy2"
RECONNECT_DELAY = 3

# Setup Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger(__name__)

# Konfigurasi Senjata & Item (Dari Strategy Brain)
WEAPONS = {
    "fist": {"bonus": 0, "range": 0},
    "dagger": {"bonus": 10, "range": 0},
    "sword": {"bonus": 20, "range": 0},
    "katana": {"bonus": 35, "range": 0},
    "bow": {"bonus": 5, "range": 1},
    "pistol": {"bonus": 10, "range": 1},
    "sniper": {"bonus": 28, "range": 2},
}

RECOVERY_ITEMS = {
    "medkit": 50, "bandage": 30, "emergency_food": 20,
    "energy_drink": 0,
}

def get_weapon_bonus(equipped_weapon) -> int:
    if not equipped_weapon:
        return 0
    type_id = equipped_weapon.get("typeId", "").lower()
    return WEAPONS.get(type_id, {}).get("bonus", 0)

def get_weapon_range(equipped_weapon) -> int:
    if not equipped_weapon:
        return 0
    type_id = equipped_weapon.get("typeId", "").lower()
    return WEAPONS.get(type_id, {}).get("range", 0)

def _find_safe_region(connections, danger_ids: set, view: dict) -> str | None:
    """Mencari region terdekat yang aman dari Death Zone."""
    safe_regions = []
    for conn in connections:
        if isinstance(conn, str):
            if conn not in danger_ids:
                safe_regions.append((conn, 0))
        elif isinstance(conn, dict):
            rid = conn.get("id", "")
            is_dz = conn.get("isDeathZone", False)
            if rid and not is_dz and rid not in danger_ids:
                terrain = conn.get("terrain", "").lower()
                score = {"hills": 3, "plains": 2, "ruins": 1, "forest": 0, "water": -2}.get(terrain, 0)
                safe_regions.append((rid, score))

    if safe_regions:
        safe_regions.sort(key=lambda x: x[1], reverse=True)
        return safe_regions[0][0]

    for conn in connections:
        rid = conn if isinstance(conn, str) else conn.get("id", "")
        if rid: return rid
    return None

def decide_action(view: dict) -> dict | None:
    """
    Brain Logic Utama dengan sistem prioritas v1.5.2.
    """
    self_data = view.get("self", {})
    region = view.get("currentRegion", {})
    hp = self_data.get("hp", 100)
    ep = self_data.get("ep", 10)
    inventory = self_data.get("inventory", [])
    equipped = self_data.get("equippedWeapon")
    is_alive = self_data.get("isAlive", True)

    visible_agents = view.get("visibleAgents", [])
    visible_monsters = view.get("visibleMonsters", [])
    visible_items_raw = view.get("visibleItems", [])
    connected_regions = view.get("connectedRegions", [])
    pending_dz = view.get("pendingDeathzones", [])
    
    connections = connected_regions or region.get("connections", [])
    region_id = region.get("id", "")

    if not is_alive:
        return None

    # Pemetaan bahaya (Death Zones)
    danger_ids = set()
    for dz in pending_dz:
        if isinstance(dz, dict):
            danger_ids.add(dz.get("id", ""))
        elif isinstance(dz, str):
            danger_ids.add(dz)
            
    for conn in connections:
        if isinstance(conn, dict) and conn.get("isDeathZone"):
            danger_ids.add(conn.get("id", ""))

    # PRIORITAS 1: Keluar dari Death Zone
    if region.get("isDeathZone", False) or region_id in danger_ids:
        safe = _find_safe_region(connections, danger_ids, view)
        if safe and ep >= 2:
            log.warning(f"🚨 ESCAPE: Menghindari Death Zone! Pindah ke {safe[:8]} (HP={hp})")
            return {"action": "move", "data": {"regionId": safe}}

    # FREE ACTION: Auto-Equip Senjata Terbaik
    current_bonus = get_weapon_bonus(equipped)
    best_weapon = None
    for item in inventory:
        if isinstance(item, dict) and item.get("category") == "weapon":
            bonus = WEAPONS.get(item.get("typeId", "").lower(), {}).get("bonus", 0)
            if bonus > current_bonus:
                best_weapon = item
                current_bonus = bonus
    if best_weapon:
        log.info(f"EQUIP: Memakai {best_weapon.get('typeId')} (+{current_bonus} ATK)")
        return {"action": "equip", "data": {"itemId": best_weapon["id"]}}

    # PRIORITAS 3: Healing
    if hp < 70:
        heals = [i for i in inventory if isinstance(i, dict) and i.get("typeId", "").lower() in RECOVERY_ITEMS]
        if heals:
            # Urutkan berdasarkan efektivitas (critical vs normal)
            if hp < 30:
                heals.sort(key=lambda i: RECOVERY_ITEMS.get(i.get("typeId", "").lower(), 0), reverse=True)
            else:
                heals.sort(key=lambda i: RECOVERY_ITEMS.get(i.get("typeId", "").lower(), 0))
            
            heal_item = heals[0]
            log.info(f"HEAL: HP={hp}, menggunakan {heal_item.get('typeId')}")
            return {"action": "use_item", "data": {"itemId": heal_item["id"]}}

    # PRIORITAS 5 & 6: Pertarungan (Guardian & Agent)
    enemies = [a for a in visible_agents if a.get("isAlive", True) and a.get("id") != self_data.get("id")]
    if enemies and ep >= 2 and hp >= 40:
        target = min(enemies, key=lambda t: t.get("hp", 999))
        target_region = target.get("regionId", region_id)
        
        if target_region == region_id:
            log.info(f"COMBAT: Menyerang agent/guardian (HP={target.get('hp', '?')})")
            return {"action": "attack", "data": {"targetId": target["id"], "targetType": "agent"}}

    # PRIORITAS 7: Monster Farming
    monsters = [m for m in visible_monsters if m.get("hp", 0) > 0]
    if monsters and ep >= 2:
        target = min(monsters, key=lambda t: t.get("hp", 999))
        if target.get("regionId", region_id) == region_id:
            log.info(f"FARMING: Menyerang monster (HP={target.get('hp', '?')})")
            return {"action": "attack", "data": {"targetId": target["id"], "targetType": "monster"}}

    # PRIORITAS 9: Pergerakan Strategis (Eksplorasi)
    if ep >= 2 and connections:
        safe_target = _find_safe_region(connections, danger_ids, view)
        if safe_target:
            return {"action": "move", "data": {"regionId": safe_target}}

    # PRIORITAS 10: Rest
    if ep < 4 and not region.get("isDeathZone") and region_id not in danger_ids:
        log.info(f"REST: Memulihkan EP (Saat ini {ep})")
        return {"action": "rest", "data": {}}

    return None


async def play_claw_royale():
    try:
        response = requests.get(f"{BASE_API_URL}/version", timeout=5)
        current_version = response.json().get("version", "1.15.0")
    except Exception as e:
        log.error(f"Gagal mengambil versi: {e}")
        return

    headers = {"X-Version": current_version, "X-API-Key": API_KEY}
    log.info(f"Menghubungkan ke {WS_JOIN_URL} dengan versi {current_version}...")

    async with websockets.connect(WS_JOIN_URL, additional_headers=headers) as ws:
        await ws.recv()
        await ws.send(json.dumps({"type": "hello", "entryType": "free"}))
        log.info("Hello payload terkirim. Menunggu event permainan...")

        async for message in ws:
            event = json.loads(message)
            event_type = event.get("type")

            if event_type == "agent_died":
                if event.get("meta", {}).get("youDied") is True:
                    log.warning("Agen mati! Menyiapkan Auto Rejoin...")
                    break
            elif event_type == "game_ended":
                log.info("Permainan selesai! Menyiapkan Auto Rejoin...")
                break
            elif event_type in ["agent_view", "game_state"]:
                view_data = event.get("data", event)
                action_payload = decide_action(view_data)
                
                if action_payload:
                    payload = {"type": "action", **action_payload}
                    await ws.send(json.dumps(payload))

async def main():
    while True:
        try:
            await play_claw_royale()
        except websockets.exceptions.ConnectionClosed as e:
            log.error(f"Koneksi terputus: {e}")
        except Exception as e:
            log.error(f"Terjadi kesalahan: {e}")

        log.info(f"Rejoining dalam {RECONNECT_DELAY} detik...\n")
        await asyncio.sleep(RECONNECT_DELAY)

if __name__ == "__main__":
    asyncio.run(main())
