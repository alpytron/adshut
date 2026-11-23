// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Alpar Sandor
package adshut.app;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.os.Handler;

/**
 * Invisible stepping stone for starting the service after boot.
 *
 * Android 11 gives a foreground service camera access only if it was started while its app was
 * visible. A boot receiver runs in the background, so it opens this transparent activity, which
 * starts the service and closes again a moment later.
 */
public class StartActivity extends Activity {
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        startService(new Intent(this, CamService.class));
        new Handler().postDelayed(new Runnable() {
            @Override
            public void run() {
                finish();
                overridePendingTransition(0, 0);
            }
        }, 1500);   // stay visible until the service has gone to the foreground
    }
}
