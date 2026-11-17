package world.voidcraft.tldrack

import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.viewModels
import world.voidcraft.tldrack.laya.LayaRules
import world.voidcraft.tldrack.laya.LayaScreen
import world.voidcraft.tldrack.laya.LayaViewModel
import world.voidcraft.tldrack.ui.TldrackTheme

/** The app is one screen: LAYA's review list. A launch, the launcher icon and `tldrack://review` all land on it. */
class MainActivity : ComponentActivity() {

    private val vm: LayaViewModel by viewModels()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContent {
            TldrackTheme {
                LayaScreen(vm)
            }
        }
    }

    /**
     * The link while the app is already up (`singleTop`). A resume alone would keep a page that landed seconds ago;
     * a push saying cards are waiting is reason to ask again.
     */
    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        if (LayaRules.is_review_link(intent.action, intent.data?.scheme, intent.data?.host)) vm.load()
    }
}
