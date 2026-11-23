// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Alpar Sandor
package adshut.app;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.util.Log;

/** Starts the capture service after the TV boots (or on the adshut.app.START broadcast). */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        // This alone gets the service running, but without camera access (see StartActivity).
        context.startService(new Intent(context, CamService.class));
        try {
            // Starting an activity from the background needs the "display over other apps"
            // permission, granted once over ADB.
            context.startActivity(new Intent(context, StartActivity.class)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_NO_ANIMATION));
        } catch (RuntimeException e) {
            Log.w("adshut", "could not open the start activity: " + e);
        }
    }
}
