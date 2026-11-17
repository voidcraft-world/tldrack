package world.voidcraft.tldrack

import kotlinx.serialization.json.Json

/**
 * One JSON configuration for everything this app encodes: the wire and the saved
 * reading state. `explicitNulls = false` is load-bearing on the wire: an absent
 * optional must be OMITTED, because a reader that types a key as optional refuses null.
 *
 * Its own file, apart from Prefs.kt, so JVM unit tests that touch JSON never
 * load the DataStore delegate.
 */
val APP_JSON = Json {
    ignoreUnknownKeys = true
    explicitNulls = false
}
