package world.voidcraft.tldrack

import android.content.Context
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.preferencesDataStore

val Context.tldrack_prefs: DataStore<Preferences> by preferencesDataStore(name = "tldrack")

object PrefKeys {
    /** LAYA's TO GRADE; absent reads as ON, because grading is why the screen is opened. */
    val laya_to_grade = booleanPreferencesKey("laya_to_grade")
}
